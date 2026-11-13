"""Tests for the built-in ``ModelRouter``, with scripted router models."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx
import pytest
from a2a.helpers import new_text_part
from a2a.types import Message, Part, Role
from pydantic import BaseModel, Field

from agentframe import (
    Agent,
    AgentApp,
    AllowAnonymous,
    Clarification,
    HostConfig,
    InputValidationError,
    ModelCapabilities,
    ModelRefusalError,
    ModelRouter,
    ModelSettings,
    RoutingContext,
    RunContext,
    RunLimits,
    SkillCall,
    UnsupportedCapabilityError,
    agent,
    skill,
)
from agentframe.hosting.model_router import NO_TEXT_QUESTION, extraction_output
from agentframe.hosting.routing import new_routing_context
from agentframe.runtime.generation import RunAccounting
from agentframe.testing import ScriptedModel
from local_server import ServeLocally


class Glossary(BaseModel):
    terms: list[str]


@agent(version="1.0.0")
class Linguist(Agent):
    """Summarizes and translates text."""

    instructions = "Agent instructions are for skills only."
    limits = RunLimits(timeout_seconds=2)

    @skill("summarize", tags=["text"])
    async def summarize(self, text: str, ctx: RunContext) -> str:
        """Summarize text in a sentence."""
        return await ctx.generate(text)

    @skill("translate", tags=["text", "languages"])
    async def translate(
        self,
        text: str,
        language: str,
        ctx: RunContext,
        formal: bool = False,
        times: Annotated[int, Field(ge=1)] = 1,
        glossary: Glossary | None = None,
    ) -> str:
        """Translate text into another language."""
        return f"[{language}, formal={formal}, x{times}] {text}"


SKILLS = list(Linguist.definition().skills.values())
SUMMARIZE, TRANSLATE = SKILLS


def router_model(*replies: object) -> ScriptedModel:
    """A router model that answers with ``replies`` as JSON."""
    return ScriptedModel(
        [json.dumps(reply) for reply in replies],
        capabilities=ModelCapabilities(context_window=8192, structured_output=True),
    )


def select(skill: str | None, question: str | None = None) -> dict[str, Any]:
    return {"skill": skill, "question": question}


def extract(
    arguments: dict[str, Any] | None, question: str | None = None
) -> dict[str, Any]:
    return {"arguments": arguments, "question": question}


def user(text: str) -> Message:
    return Message(
        message_id=str(uuid.uuid4()), role=Role.ROLE_USER, parts=[new_text_part(text)]
    )


def agent_question(text: str) -> Message:
    return Message(
        message_id=str(uuid.uuid4()), role=Role.ROLE_AGENT, parts=[new_text_part(text)]
    )


def routing_context(accounting: RunAccounting | None = None) -> RoutingContext:
    return new_routing_context(
        Linguist(),
        "run-1",
        None,
        asyncio.get_running_loop().time(),
        accounting or RunAccounting(),
    )


async def route(
    model: ScriptedModel, *conversation: Message, skills: list[Any] = SKILLS
) -> SkillCall | Clarification:
    return await ModelRouter(model).route(conversation, skills, routing_context())


# Selection and extraction


async def test_routes_to_the_selected_skill_with_extracted_arguments() -> None:
    model = router_model(
        select("translate"), extract({"text": "Hello", "language": "French"})
    )

    call = await route(model, user("Translate 'Hello' into French"))

    assert call == SkillCall("translate", {"text": "Hello", "language": "French"})


async def test_unmentioned_optional_arguments_are_left_for_the_skills_defaults() -> (
    None
):
    model = router_model(
        select("translate"),
        extract(
            {
                "text": "Hello",
                "language": "French",
                "formal": None,
                "times": None,
                "glossary": None,
            }
        ),
    )

    call = await route(model, user("Translate 'Hello' into French"))

    assert call == SkillCall("translate", {"text": "Hello", "language": "French"})


async def test_optional_arguments_the_caller_gave_are_kept() -> None:
    model = router_model(
        select("translate"),
        extract({"text": "Hi", "language": "German", "formal": True, "times": 2}),
    )

    call = await route(model, user("Formally translate 'Hi' into German twice"))

    assert call == SkillCall(
        "translate", {"text": "Hi", "language": "German", "formal": True, "times": 2}
    )


@pytest.mark.parametrize(
    "replies",
    [
        [select(None, "Summarize or translate?")],
        [select("translate", "Summarize or translate?")],
        [select("translate"), extract(None, "Summarize or translate?")],
        [
            select("translate"),
            extract({"text": "Hi", "language": None}, "Summarize or translate?"),
        ],
    ],
    ids=[
        "selection-asks",
        "selection-asks-despite-a-skill",
        "extraction-asks",
        "extraction-asks-despite-arguments",
    ],
)
async def test_a_question_from_the_model_is_asked_and_never_guessed_past(
    replies: list[dict[str, Any]],
) -> None:
    model = router_model(*replies)

    decision = await route(model, user("Do the thing with 'Hi'"))

    assert decision == Clarification("Summarize or translate?")


async def test_selection_runs_even_with_one_skill_so_off_topic_requests_are_asked() -> (
    None
):
    model = router_model(select(None, "I can only summarize text. What should I?"))

    decision = await route(model, user("What's the weather?"), skills=[SUMMARIZE])

    assert decision == Clarification("I can only summarize text. What should I?")
    assert len(model.requests) == 1


@pytest.mark.parametrize(
    "replies",
    [[select(None)], [select("translate"), extract(None)], [select("summarize", " ")]],
    ids=["selection", "extraction", "blank-question"],
)
async def test_a_reply_with_neither_answer_nor_question_still_asks(
    replies: list[dict[str, Any]],
) -> None:
    model = router_model(*replies)

    decision = await route(model, user("Translate"))

    assert isinstance(decision, Clarification)
    assert decision.question


async def test_the_selection_schema_offers_only_the_given_skills() -> None:
    model = router_model(select(None, "Did you want a summary?"))

    await route(model, user("Hi"), skills=[SUMMARIZE])

    [request] = model.requests
    assert request.output is not None
    skill_schema = request.output.json_schema["properties"]["skill"]
    assert skill_schema["anyOf"] == [
        {"const": "summarize", "type": "string"},
        {"type": "null"},
    ]


async def test_a_skill_outside_the_given_skills_fails_and_is_never_returned() -> None:
    model = router_model(select("translate"))

    with pytest.raises(Exception, match="not a valid 'Selection'"):
        await route(model, user("Translate 'Hi'"), skills=[SUMMARIZE])


# Validation of extracted arguments


@pytest.mark.parametrize(
    ("arguments", "question"),
    [
        (
            {"text": "Hi", "language": "French", "times": 0},
            "Could you give valid values for: times?",
        ),
        ({"text": "Hi"}, "Could you give valid values for: language?"),
        (
            {"text": "Hi", "language": 3, "glossary": {"terms": "x"}},
            "Could you give valid values for: language, glossary?",
        ),
    ],
    ids=["constraint", "missing", "wrong-types"],
)
async def test_invalid_extracted_arguments_ask_naming_only_the_fields(
    arguments: dict[str, Any], question: str
) -> None:
    model = router_model(select("translate"), extract(arguments))

    decision = await route(model, user("Translate 'Hi'"))

    assert decision == Clarification(question)


async def test_made_up_argument_names_are_not_echoed() -> None:
    model = router_model(
        select("summarize"), extract({"text": "Hi", "secret-key-123": 1})
    )

    decision = await route(model, user("Summarize 'Hi'"))

    assert isinstance(decision, Clarification)
    assert "secret-key-123" not in decision.question


def test_the_extraction_schema_makes_optional_arguments_nullable_and_required() -> None:
    schema = extraction_output(TRANSLATE).model_json_schema()

    [arguments, null] = schema["properties"]["arguments"]["anyOf"]
    assert null == {"type": "null"}
    assert sorted(arguments["required"]) == sorted(TRANSLATE.argument_names)
    assert {"type": "null"} in arguments["properties"]["formal"]["anyOf"]
    assert "default" not in json.dumps(arguments)
    assert arguments["properties"]["language"] == {
        "title": "Language",
        "type": "string",
    }
    # The nested model's definition is at the root, where its reference points.
    assert "Glossary" in schema["$defs"]


# What the model sees


async def test_skill_descriptions_are_only_trusted_and_caller_text_only_user() -> None:
    model = router_model(
        select("translate"), extract({"text": "Hello", "language": "French"})
    )
    conversation = [
        user("Translate something"),
        agent_question("What should I translate, and into which language?"),
        user("'Hello' into French"),
    ]
    router = ModelRouter(model, instructions="Prefer translation when unsure.")

    await router.route(conversation, SKILLS, routing_context())

    for request in model.requests:
        system, *rest = request.messages
        assert system.role == "system"
        assert "Prefer translation when unsure." in system.content
        assert "Agent instructions" not in system.content
        assert "Translate text into another language." in system.content
        assert "Translate something" not in system.content
        assert [(m.role, m.content) for m in rest] == [
            ("user", "Translate something"),
            ("assistant", "What should I translate, and into which language?"),
            ("user", "'Hello' into French"),
        ]
    selection, extraction = model.requests
    assert "Summarize text in a sentence." in selection.messages[0].content
    assert "Summarize text in a sentence." not in extraction.messages[0].content


async def test_non_text_parts_are_ignored() -> None:
    model = router_model(select(None, "What should I do with the file?"))
    message = user("Summarize this")
    message.parts.append(Part(url="https://example.com/file.pdf"))

    await route(model, message)

    [request] = model.requests
    assert request.messages[-1].content == "Summarize this"


async def test_a_message_without_text_is_asked_about_without_a_model_call() -> None:
    model = router_model()
    message = Message(
        message_id="m1",
        role=Role.ROLE_USER,
        parts=[Part(url="https://example.com/file.pdf")],
    )

    decision = await route(model, message)

    assert decision == Clarification(NO_TEXT_QUESTION)
    assert model.requests == []


# Failures


async def test_no_eligible_skills_fails_without_a_model_call() -> None:
    model = router_model()

    with pytest.raises(InputValidationError, match="No skill is available"):
        await route(model, user("Hi"), skills=[])

    assert model.requests == []


async def test_a_model_without_structured_output_fails_before_sending() -> None:
    model = ScriptedModel(["{}"])

    with pytest.raises(UnsupportedCapabilityError):
        await route(model, user("Hi"))

    assert model.requests == []


async def test_model_errors_propagate() -> None:
    model = ScriptedModel(
        [json.dumps(select("summarize")), ModelRefusalError("Refused")],
        capabilities=ModelCapabilities(context_window=8192, structured_output=True),
    )

    with pytest.raises(ModelRefusalError):
        await route(model, user("Summarize 'Hi'"))


async def test_both_calls_are_charged_to_the_run_with_the_routers_settings() -> None:
    accounting = RunAccounting()
    model = router_model(select("summarize"), extract({"text": "Hi"}))
    router = ModelRouter(model, model_settings=ModelSettings(max_output_tokens=64))

    await router.route([user("Summarize 'Hi'")], SKILLS, routing_context(accounting))

    assert accounting.model_calls == 2
    assert [r.settings.max_output_tokens for r in model.requests] == [64, 64]


# Hosted: accounting and follow-up answers


@asynccontextmanager
async def serve_linguist(
    serve: ServeLocally, router: ModelRouter, skill_model: ScriptedModel
) -> AsyncIterator[httpx.AsyncClient]:
    def make_app(public_url: str) -> AgentApp:
        return AgentApp(
            agent=Linguist(model=skill_model),
            # Test-only: anonymous access on a loopback test server.
            host=HostConfig(public_url=public_url, auth=AllowAnonymous()),
            router=router,
        )

    async with serve(make_app) as base_url, httpx.AsyncClient(base_url=base_url) as h:
        yield h


async def say(
    http: httpx.AsyncClient, text: str, task_id: str | None = None
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "messageId": str(uuid.uuid4()),
        "role": "ROLE_USER",
        "parts": [{"text": text}],
    }
    if task_id is not None:
        message["taskId"] = task_id
    body = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {"message": message},
    }
    response = await http.post("/a2a", json=body, headers={"A2A-Version": "1.0"})
    task: dict[str, Any] = response.json()["result"]["task"]
    return task


def state(task: Mapping[str, Any]) -> str:
    value: str = task["status"]["state"]
    return value


async def test_a_routed_run_reports_both_routing_calls_plus_the_skills(
    serve: ServeLocally,
) -> None:
    router = ModelRouter(router_model(select("summarize"), extract({"text": "Hi"})))
    skill_model = ScriptedModel(["A greeting."])

    async with serve_linguist(serve, router, skill_model) as h:
        task = await say(h, "Summarize 'Hi'")

    assert state(task) == "TASK_STATE_COMPLETED"
    assert task["metadata"]["usage"]["model_calls"] == 3
    assert len(skill_model.requests) == 1


async def test_a_follow_up_answer_is_routed_again_with_the_whole_conversation(
    serve: ServeLocally,
) -> None:
    model = router_model(
        select("translate"),
        extract(None, "Into which language?"),
        select("translate"),
        extract({"text": "Hi", "language": "French"}),
    )

    async with serve_linguist(serve, ModelRouter(model), ScriptedModel()) as h:
        asked = await say(h, "Translate 'Hi'")
        answered = await say(h, "French", task_id=asked["id"])

    assert state(asked) == "TASK_STATE_INPUT_REQUIRED"
    assert state(answered) == "TASK_STATE_COMPLETED"
    assert answered["artifacts"][0]["parts"][0]["text"] == (
        "[French, formal=False, x1] Hi"
    )
    last_extraction = model.requests[-1]
    assert [(m.role, m.content) for m in last_extraction.messages[1:]] == [
        ("user", "Translate 'Hi'"),
        ("assistant", "Into which language?"),
        ("user", "French"),
    ]
