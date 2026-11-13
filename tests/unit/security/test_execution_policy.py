"""Tests for ``ExecutionPolicy`` input, model-request and output checks.

Models are scripted, so no test calls a provider. Hosted tests travel over
real localhost HTTP to an ``AgentApp`` served by Uvicorn.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from agentframe import (
    Agent,
    AgentApp,
    AllowAnonymous,
    DefinitionError,
    ExecutionPolicy,
    HostConfig,
    ModelCapabilities,
    ModelRequest,
    ModelRouter,
    PermissionDeniedError,
    PolicyUnavailableError,
    Principal,
    ProviderError,
    RunContext,
    RunLimits,
    Runtime,
    SkillDefinition,
    TimeoutLimitExceededError,
    ToolCall,
    agent,
    skill,
    tool,
)
from agentframe.hosting.card import SKILL_CALL_EXTENSION_URI
from agentframe.runtime import generation
from agentframe.testing import ScriptedModel
from local_server import ServeLocally

SECRET = "the-secret-word"

TOOL_MODEL = ModelCapabilities(
    context_window=128_000, structured_output=True, tools=True
)


class Answer(BaseModel):
    text: str


@tool(description="Look up a word.")
def lookup(word: str) -> str:
    return f"definition of {word}"


@agent(version="1.0.0")
class Writer(Agent):
    """Writes with and without a model."""

    limits = RunLimits(timeout_seconds=1)

    def __init__(self, model: ScriptedModel | None = None) -> None:
        super().__init__(model=model, tools=[lookup])
        self.ran: list[str] = []

    @skill("echo", tags=["test"])
    async def echo(self, text: str, ctx: RunContext) -> str:
        """Echo text without a model."""
        self.ran.append("echo")
        return text

    @skill("write", tags=["test"])
    async def write(self, text: str, ctx: RunContext) -> str:
        """Ask the model."""
        self.ran.append("write")
        return await ctx.generate(text)

    @skill("answer", tags=["test"])
    async def answer(self, text: str, ctx: RunContext) -> Answer:
        """Ask the model for a structured answer."""
        return await ctx.generate(text, output=Answer)

    @skill("define", tags=["test"])
    async def define(self, text: str, ctx: RunContext) -> str:
        """Ask the model, which may look words up."""
        return await ctx.generate(text, tools=[lookup])

    @skill("try_write", tags=["test"])
    async def try_write(self, text: str, ctx: RunContext) -> str:
        """Ask the model, falling back when the request is blocked."""
        try:
            return await ctx.generate(text)
        except PermissionDeniedError:
            return "blocked"


class Recorder:
    """A check that records what it saw and answers with ``decision``."""

    def __init__(self, decision: object = True) -> None:
        self.decision = decision
        self.seen: list[tuple[SkillDefinition | None, Principal | None, Any]] = []

    async def __call__(
        self, skill: SkillDefinition | None, principal: Principal | None, content: Any
    ) -> bool:
        self.seen.append((skill, principal, content))
        return self.decision  # type: ignore[return-value]


async def broken(
    skill: SkillDefinition | None, principal: object, content: Any
) -> bool:
    raise RuntimeError(f"policy service down while checking {content!r}")


async def hangs(skill: SkillDefinition | None, principal: object, content: Any) -> bool:
    await asyncio.sleep(10)
    return True


def skill_ids(recorder: Recorder) -> list[str | None]:
    return [skill.id if skill else None for skill, _, _ in recorder.seen]


# With no policy


async def test_without_a_policy_runs_are_unchanged() -> None:
    model = ScriptedModel(["Hi!"])

    value = await Runtime().invoke(
        Writer(model), skill="write", arguments={"text": "Hello"}
    )

    assert value == "Hi!"


# Input checks


async def test_an_allowing_input_check_sees_the_validated_arguments() -> None:
    check = Recorder()
    alice = Principal(id="alice")
    runtime = Runtime(policy=ExecutionPolicy(input_checks=[check]))

    value = await runtime.invoke(
        Writer(), skill="echo", arguments={"text": "Hi"}, principal=alice
    )

    assert value == "Hi"
    [(skill, principal, arguments)] = check.seen
    assert skill is not None and skill.id == "echo"
    assert principal == alice
    assert arguments == {"text": "Hi"}
    with pytest.raises(TypeError):
        arguments["text"] = "changed"  # Read-only.


async def test_a_denying_input_check_stops_the_run_before_the_skill() -> None:
    writer = Writer()
    runtime = Runtime(policy=ExecutionPolicy(input_checks=[Recorder(False)]))

    with pytest.raises(PermissionDeniedError) as denied:
        await runtime.invoke(writer, skill="echo", arguments={"text": SECRET})

    assert denied.value.message == (
        "Skill 'echo' was blocked by an execution policy check"
    )
    assert denied.value.invocation_id is not None
    assert writer.ran == []


async def test_input_checks_run_in_order_and_stop_at_the_first_failure() -> None:
    first, second, third = Recorder(), Recorder(False), Recorder()
    runtime = Runtime(policy=ExecutionPolicy(input_checks=[first, second, third]))

    with pytest.raises(PermissionDeniedError):
        await runtime.invoke(Writer(), skill="echo", arguments={"text": "Hi"})

    assert (len(first.seen), len(second.seen), len(third.seen)) == (1, 1, 0)


async def test_input_checks_run_after_the_authorization_policy() -> None:
    class DenyAll:
        async def check(self, *args: object) -> bool:
            return False

    check = Recorder()
    runtime = Runtime(
        authorization=DenyAll(), policy=ExecutionPolicy(input_checks=[check])
    )

    with pytest.raises(PermissionDeniedError, match="authorization policy"):
        await runtime.invoke(Writer(), skill="echo", arguments={"text": "Hi"})

    assert check.seen == []


async def test_input_checks_see_only_valid_arguments() -> None:
    check = Recorder()
    runtime = Runtime(policy=ExecutionPolicy(input_checks=[check]))

    with pytest.raises(Exception, match="Invalid arguments"):
        await runtime.invoke(Writer(), skill="echo", arguments={})

    assert check.seen == []


# Fail closed: shared by every check point


@pytest.mark.parametrize("decision", [False, None, "yes", 1])
async def test_only_an_explicit_true_allows(decision: object) -> None:
    runtime = Runtime(policy=ExecutionPolicy(input_checks=[Recorder(decision)]))

    with pytest.raises(PermissionDeniedError):
        await runtime.invoke(Writer(), skill="echo", arguments={"text": "Hi"})


async def test_a_check_that_raises_fails_closed_without_revealing_content() -> None:
    writer = Writer()
    runtime = Runtime(policy=ExecutionPolicy(input_checks=[broken]))

    with pytest.raises(PolicyUnavailableError) as unavailable:
        await runtime.invoke(writer, skill="echo", arguments={"text": SECRET})

    assert unavailable.value.message == (
        "Skill 'echo' could not be checked: an execution policy check is unavailable"
    )
    assert SECRET not in str(unavailable.value)
    assert writer.ran == []


async def test_a_check_still_running_at_the_deadline_fails_closed() -> None:
    writer = Writer()
    runtime = Runtime(policy=ExecutionPolicy(input_checks=[hangs]))

    with pytest.raises(PolicyUnavailableError):
        await asyncio.wait_for(
            runtime.invoke(writer, skill="echo", arguments={"text": "Hi"}), 3
        )

    assert writer.ran == []


# Model-request checks


async def test_model_request_checks_see_the_skill_and_the_complete_request() -> None:
    check = Recorder()
    model = ScriptedModel(["Hi!"])
    runtime = Runtime(policy=ExecutionPolicy(model_request_checks=[check]))

    await runtime.invoke(Writer(model), skill="write", arguments={"text": "Hello"})

    [(skill, _, request)] = check.seen
    assert skill is not None and skill.id == "write"
    assert isinstance(request, ModelRequest)
    assert request == model.requests[0]


async def test_a_denied_model_request_is_never_sent_and_reserves_nothing() -> None:
    model = ScriptedModel(["never sent"])
    runtime = Runtime(policy=ExecutionPolicy(model_request_checks=[Recorder(False)]))

    result = await runtime.run(
        Writer(model), skill="try_write", arguments={"text": SECRET}
    )

    assert result.value == "blocked"
    assert model.requests == []
    assert result.model_calls == 0
    assert result.usage.total_tokens == 0


async def test_a_denied_model_request_fails_the_run_with_a_generic_message() -> None:
    model = ScriptedModel(["never sent"])
    runtime = Runtime(policy=ExecutionPolicy(model_request_checks=[Recorder(False)]))

    with pytest.raises(PermissionDeniedError) as denied:
        await runtime.invoke(Writer(model), skill="write", arguments={"text": SECRET})

    assert denied.value.message == (
        "Skill 'write' was blocked by an execution policy check"
    )
    assert model.requests == []


async def test_a_broken_model_request_check_fails_closed() -> None:
    model = ScriptedModel(["never sent"])
    runtime = Runtime(policy=ExecutionPolicy(model_request_checks=[broken]))

    with pytest.raises(PolicyUnavailableError):
        await runtime.invoke(Writer(model), skill="write", arguments={"text": "Hi"})

    assert model.requests == []


async def test_a_model_request_check_past_the_deadline_never_sends() -> None:
    model = ScriptedModel(["never sent"])
    runtime = Runtime(policy=ExecutionPolicy(model_request_checks=[hangs]))

    # The check runs inside the skill, whose own timeout expires at the same
    # moment, so the run reports that timeout. Either way it fails closed.
    with pytest.raises(TimeoutLimitExceededError):
        await asyncio.wait_for(
            runtime.invoke(Writer(model), skill="write", arguments={"text": "Hi"}), 3
        )

    assert model.requests == []


async def test_structured_output_requests_are_checked() -> None:
    check = Recorder(False)
    model = ScriptedModel(['{"text": "Hi"}'], capabilities=TOOL_MODEL)
    runtime = Runtime(policy=ExecutionPolicy(model_request_checks=[check]))

    with pytest.raises(PermissionDeniedError):
        await runtime.invoke(Writer(model), skill="answer", arguments={"text": "Hi"})

    [(_, _, request)] = check.seen
    assert request.output is not None and request.output.name == "Answer"
    assert model.requests == []


async def test_every_tool_loop_continuation_is_checked() -> None:
    check = Recorder()
    model = ScriptedModel(
        [ToolCall(id="call-1", name="lookup", arguments='{"word": "cat"}'), "A pet."],
        capabilities=TOOL_MODEL,
    )
    runtime = Runtime(policy=ExecutionPolicy(model_request_checks=[check]))

    value = await runtime.invoke(
        Writer(model), skill="define", arguments={"text": "Define cat"}
    )

    assert value == "A pet."
    requests = [request for _, _, request in check.seen]
    assert requests == model.requests
    assert requests[1].messages[-1].content == '"definition of cat"'


async def test_a_denied_continuation_is_never_sent() -> None:
    decisions = iter([True, False])

    async def first_only(skill: object, principal: object, content: object) -> bool:
        return next(decisions)

    model = ScriptedModel(
        [ToolCall(id="call-1", name="lookup", arguments='{"word": "cat"}'), "unsent"],
        capabilities=TOOL_MODEL,
    )
    runtime = Runtime(policy=ExecutionPolicy(model_request_checks=[first_only]))

    with pytest.raises(PermissionDeniedError):
        await runtime.invoke(
            Writer(model), skill="define", arguments={"text": "Define cat"}
        )

    assert len(model.requests) == 1


async def test_a_retried_request_is_checked_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_wait(seconds: float) -> None:
        pass

    monkeypatch.setattr(generation, "wait_before_retry", no_wait)
    check = Recorder()
    model = ScriptedModel([ProviderError("busy", transient=True), "Hi!"])
    runtime = Runtime(policy=ExecutionPolicy(model_request_checks=[check]))

    value = await runtime.invoke(
        Writer(model), skill="write", arguments={"text": "Hello"}
    )

    assert value == "Hi!"
    assert len(model.requests) == 2
    assert len(check.seen) == 1


# Output checks


async def test_an_allowing_output_check_sees_the_validated_value() -> None:
    check = Recorder()
    model = ScriptedModel(['{"text": "Hi"}'], capabilities=TOOL_MODEL)
    runtime = Runtime(policy=ExecutionPolicy(output_checks=[check]))

    value = await runtime.invoke(Writer(model), skill="answer", arguments={"text": "?"})

    assert value == Answer(text="Hi")
    [(skill, _, checked)] = check.seen
    assert skill is not None and skill.id == "answer"
    assert checked == Answer(text="Hi")


async def test_a_denying_output_check_returns_no_value() -> None:
    writer = Writer()
    runtime = Runtime(policy=ExecutionPolicy(output_checks=[Recorder(False)]))

    with pytest.raises(PermissionDeniedError) as denied:
        await runtime.invoke(writer, skill="echo", arguments={"text": SECRET})

    assert writer.ran == ["echo"]
    assert SECRET not in str(denied.value)


async def test_a_broken_output_check_fails_closed() -> None:
    runtime = Runtime(policy=ExecutionPolicy(output_checks=[broken]))

    with pytest.raises(PolicyUnavailableError):
        await runtime.invoke(Writer(), skill="echo", arguments={"text": "Hi"})


async def test_an_output_check_past_the_deadline_fails_closed() -> None:
    runtime = Runtime(policy=ExecutionPolicy(output_checks=[hangs]))

    with pytest.raises(PolicyUnavailableError):
        await asyncio.wait_for(
            runtime.invoke(Writer(), skill="echo", arguments={"text": "Hi"}), 3
        )


# Hosted


def anonymous_host(public_url: str) -> HostConfig:
    # Test-only: anonymous access on a loopback test server.
    return HostConfig(public_url=public_url, auth=AllowAnonymous())


@asynccontextmanager
async def serve_writer(
    serve: ServeLocally,
    writer: Writer,
    policy: ExecutionPolicy,
    router: ModelRouter | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    def make_app(public_url: str) -> AgentApp:
        return AgentApp(
            agent=writer, host=anonymous_host(public_url), policy=policy, router=router
        )

    async with serve(make_app) as base_url, httpx.AsyncClient(base_url=base_url) as h:
        yield h


async def send(
    http: httpx.AsyncClient, message: dict[str, Any], skill_call: bool
) -> dict[str, Any]:
    headers = {"A2A-Version": "1.0"}
    if skill_call:
        headers["A2A-Extensions"] = SKILL_CALL_EXTENSION_URI
    body = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {"message": message},
    }
    response = await http.post("/a2a", json=body, headers=headers)
    task: dict[str, Any] = response.json()["result"]["task"]
    return task


async def call_skill(
    http: httpx.AsyncClient, skill_id: str, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    message = {
        "messageId": str(uuid.uuid4()),
        "role": "ROLE_USER",
        "parts": [{"data": dict(arguments)}],
        "metadata": {"agentframe.skill": skill_id},
    }
    return await send(http, message, skill_call=True)


async def say(http: httpx.AsyncClient, text: str) -> dict[str, Any]:
    message = {
        "messageId": str(uuid.uuid4()),
        "role": "ROLE_USER",
        "parts": [{"text": text}],
    }
    return await send(http, message, skill_call=False)


def agentframe_logs(caplog: pytest.LogCaptureFixture) -> str:
    """Return AgentFrame's own log output, with tracebacks.

    The A2A SDK logs request bodies at debug level; that is its own choice.
    """
    formatter = logging.Formatter()
    return "\n".join(
        formatter.format(record)
        for record in caplog.records
        if record.name.startswith("agentframe")
    )


def status_text(task: Mapping[str, Any]) -> str:
    return " ".join(part["text"] for part in task["status"]["message"]["parts"])


async def test_hosted_output_denial_ends_the_task_without_its_value(
    serve: ServeLocally, caplog: pytest.LogCaptureFixture
) -> None:
    policy = ExecutionPolicy(output_checks=[Recorder(False)])

    with caplog.at_level(logging.DEBUG):
        async with serve_writer(serve, Writer(), policy) as h:
            task = await call_skill(h, "echo", {"text": SECRET})

    assert task["status"]["state"] == "TASK_STATE_REJECTED"
    assert "artifacts" not in task
    assert status_text(task) == "Skill 'echo' was blocked by an execution policy check"
    assert task["metadata"]["invocation_id"]
    assert SECRET not in json.dumps(task["status"])
    assert SECRET not in agentframe_logs(caplog)


async def test_hosted_check_outage_fails_the_task_without_revealing_content(
    serve: ServeLocally, caplog: pytest.LogCaptureFixture
) -> None:
    writer = Writer()
    policy = ExecutionPolicy(input_checks=[broken])

    with caplog.at_level(logging.DEBUG):
        async with serve_writer(serve, writer, policy) as h:
            task = await call_skill(h, "echo", {"text": SECRET})

    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert "artifacts" not in task
    assert writer.ran == []
    assert SECRET not in json.dumps(task["status"])
    assert SECRET not in agentframe_logs(caplog)


def router_model(*replies: object) -> ScriptedModel:
    return ScriptedModel(
        [json.dumps(reply) for reply in replies],
        capabilities=ModelCapabilities(context_window=8192, structured_output=True),
    )


async def test_routing_requests_are_checked_with_no_skill(serve: ServeLocally) -> None:
    check = Recorder()
    router = ModelRouter(
        router_model(
            {"skill": "echo", "question": None},
            {"arguments": {"text": "Hi"}, "question": None},
        )
    )

    async with serve_writer(
        serve, Writer(), ExecutionPolicy(model_request_checks=[check]), router
    ) as h:
        task = await say(h, "Echo 'Hi'")

    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert skill_ids(check) == [None, None]


async def test_a_denied_routing_request_stops_routing_and_no_skill_runs(
    serve: ServeLocally,
) -> None:
    writer = Writer()
    model = router_model({"skill": "echo", "question": None})

    async with serve_writer(
        serve,
        writer,
        ExecutionPolicy(model_request_checks=[Recorder(False)]),
        ModelRouter(model),
    ) as h:
        task = await say(h, SECRET)

    assert task["status"]["state"] == "TASK_STATE_REJECTED"
    assert status_text(task) == "The request was blocked by an execution policy check"
    assert task["metadata"]["usage"]["model_calls"] == 0
    assert model.requests == []
    assert writer.ran == []


def test_app_policy_cannot_be_combined_with_a_runtime() -> None:
    with pytest.raises(DefinitionError, match="policy"):
        AgentApp(
            agent=Writer(),
            host=anonymous_host("http://127.0.0.1:8000"),
            runtime=Runtime(),
            policy=ExecutionPolicy(),
        )
