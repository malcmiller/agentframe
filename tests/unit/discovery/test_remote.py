import asyncio
import itertools
import json
from collections.abc import Mapping
from typing import Any, Literal, assert_type

import httpx
import pytest
from a2a.helpers import new_data_part, new_task, new_text_artifact, new_text_part
from a2a.types import (
    Message,
    Role,
    SecurityScheme,
    SendMessageResponse,
    Task,
    TaskState,
)
from google.protobuf.json_format import MessageToDict  # type: ignore[import-untyped]
from pydantic import BaseModel

from agentframe import (
    Agent,
    AgentApp,
    AgentDirectory,
    AgentQuery,
    AgentReference,
    AgentRegistry,
    AllowAnonymous,
    APIKeyCredential,
    AuthenticationError,
    DefinitionError,
    EntraAppCredential,
    HostConfig,
    InputValidationError,
    LimitExceededError,
    OnBehalfOf,
    OutputValidationError,
    Remote,
    RemoteAgentError,
    RunContext,
    RunLimits,
    RunOptions,
    Runtime,
    Service,
    TimeoutLimitExceededError,
    agent,
    skill,
    tool,
)
from agentframe.discovery import remote
from agentframe.discovery.remote import bind_remote, remote_name
from agentframe.hosting.card import SKILL_CALL_EXTENSION_URI, build_agent_card
from local_server import lifespan_startup

ORIGIN = "https://research.example.com"
CARD_URL = f"{ORIGIN}/.well-known/agent-card.json"
A2A_URL = f"{ORIGIN}/a2a"

type Research = Remote[Literal["research-agent"]]

# Kept before the autouse fixture below replaces it with zero delays.
REAL_POLL_DELAYS = remote.poll_delays


class Topic(BaseModel):
    name: str


class Source(BaseModel):
    title: str
    pages: int


# Checked by mypy --strict in CI: both spellings type-check, ask returns str and
# call returns its output type, or Any without one.
async def _type_checks(research: Research, direct: Remote[Literal["x"]]) -> None:
    assert_type(await research.ask("Research tides"), str)
    assert_type(await direct.ask("Hello"), str)
    found = await research.call("find_sources", topic="tides", output=list[Source])
    assert_type(found, list[Source])
    assert_type(await research.call("find_sources", topic="tides"), Any)


@agent(name="Planner", description="Plans with research.", version="0.1.0")
class Planner(Agent):
    @skill(id="plan", description="Research a goal.", tags=["planning"])
    async def plan(self, goal: str, research: Research, ctx: RunContext) -> str:
        return await research.ask(goal)


class OneAgent(AgentRegistry):
    async def get(self, agent_id: str) -> AgentReference:
        raise NotImplementedError

    async def list(self, query: AgentQuery) -> list[AgentReference]:
        reference = AgentReference("research-agent", CARD_URL)
        return [reference] if reference.name in (query.names or set()) else []


def card_json(interface_url: str = A2A_URL, **extra: Any) -> dict[str, Any]:
    return {
        "name": "Research",
        "description": "Finds things out.",
        "version": "0.1.0",
        "supportedInterfaces": [
            {
                "url": interface_url,
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
            }
        ],
        "capabilities": {},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [],
        **extra,
    }


def skill_call_card(*skill_ids: str) -> dict[str, Any]:
    """A card that offers the skill-call extension and the given skills."""
    skills = [
        {"id": skill_id, "name": skill_id, "description": "A skill.", "tags": []}
        for skill_id in skill_ids
    ]
    capabilities = {"extensions": [{"uri": SKILL_CALL_EXTENSION_URI}]}
    return card_json(capabilities=capabilities, skills=skills)


def result(value: Any, state: TaskState = TaskState.TASK_STATE_COMPLETED) -> Task:
    """A finished task whose artifact is a skill result as AgentFrame sends it."""
    finished = task(state)
    finished.artifacts.add(parts=[new_data_part({"value": value})])
    return finished


def task(state: TaskState, *parts: str) -> Task:
    result = new_task("task-1", "context-1", state)
    if parts:
        result.artifacts.append(new_text_artifact("result", parts[0]))
    return result


class Peer:
    """A fake A2A agent: serves its card and answers JSON-RPC from a script.

    A reply of ``"hang"`` never answers, so the caller's deadline passes.
    """

    def __init__(
        self, *replies: Task | Message | dict[str, Any] | str, card: Any = None
    ):
        self.card = card_json() if card is None else card
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=self.card)
        rpc = json.loads(request.content)
        reply = self.replies.pop(0)
        if isinstance(reply, str):  # "hang": wait to be cancelled.
            await asyncio.sleep(60)
            raise AssertionError("the caller's deadline should have passed")
        if isinstance(reply, dict):
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": rpc["id"], **reply}
            )
        if rpc["method"] == "SendMessage":
            key = "task" if isinstance(reply, Task) else "message"
            reply = SendMessageResponse(**{key: reply})
        result = MessageToDict(reply)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": rpc["id"], "result": result}
        )

    def methods(self) -> list[str]:
        return [json.loads(r.content)["method"] for r in self.requests if r.content]

    def directory(self, *, follow_redirects: bool = False) -> AgentDirectory:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(self.handle),
            follow_redirects=follow_redirects,
        )
        return AgentDirectory(sources=[OneAgent()], http_client=client)


@pytest.fixture(autouse=True)
def no_poll_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote, "poll_delays", lambda: itertools.repeat(0.0))


async def ask(peer: Peer, goal: str = "tides") -> Any:
    runtime = Runtime(directory=peer.directory())
    return await runtime.invoke(Planner(), skill="plan", arguments={"goal": goal})


# Annotations and schemas


def test_remote_name_reads_the_literal_through_a_type_alias() -> None:
    assert remote_name(Research) == "research-agent"
    assert remote_name(Remote[Literal["x"]]) == "x"
    assert remote_name(str) is None


@pytest.mark.parametrize("annotation", [Remote, Remote[str], Remote[Literal["a", "b"]]])
def test_remote_without_one_literal_name_is_a_definition_error(
    annotation: object,
) -> None:
    with pytest.raises(DefinitionError, match=r"Remote\[Literal\['agent-name'\]\]"):
        remote_name(annotation)


def test_remote_parameters_are_not_arguments_or_in_the_card() -> None:
    [plan] = Planner.definition().skills.values()
    host = HostConfig(public_url="http://127.0.0.1:8000", auth=AllowAnonymous())
    card = build_agent_card(Planner.definition(), host)

    assert plan.remotes == {"research": "research-agent"}
    assert list(plan.arguments_adapter.json_schema()["properties"]) == ["goal"]
    schema = card.capabilities.extensions[0].params["skills"]["plan"]
    assert list(schema["properties"].keys()) == ["goal"]


async def test_a_caller_cannot_supply_a_remote_argument() -> None:
    runtime = Runtime(directory=Peer().directory())

    with pytest.raises(InputValidationError, match="research"):
        await runtime.invoke(
            Planner(), skill="plan", arguments={"goal": "x", "research": A2A_URL}
        )


def test_tools_cannot_take_remote_parameters() -> None:
    with pytest.raises(DefinitionError, match="tools cannot take Remote"):

        @tool()
        def lookup(research: Research) -> str:
            """Look something up."""
            return ""


def test_a_remote_parameter_without_a_directory_fails_at_construction() -> None:
    host = HostConfig(public_url="http://127.0.0.1:8000", auth=AllowAnonymous())

    with pytest.raises(DefinitionError, match="no directory"):
        AgentApp(agent=Planner(), host=host)


# Startup and destination checks


async def test_a_card_that_requires_credentials_is_refused() -> None:
    schemes = {"apiKey": {"apiKeySecurityScheme": {"location": "header", "name": "k"}}}
    peer = Peer(card=card_json(securitySchemes=schemes))

    with pytest.raises(DefinitionError, match="requires credentials"):
        await Runtime(directory=peer.directory()).validate_remotes(Planner())


async def test_an_interface_on_another_origin_is_refused_before_sending() -> None:
    peer = Peer(card=card_json("https://elsewhere.example.com/a2a"))

    with pytest.raises(RemoteAgentError):
        await ask(peer)

    assert [str(r.url) for r in peer.requests] == [CARD_URL]


async def test_startup_fails_for_a_card_the_directory_cannot_validate() -> None:
    peer = Peer(card={"name": "not a card"})
    app = AgentApp(
        agent=Planner(),
        host=HostConfig(public_url="http://127.0.0.1:8000", auth=AllowAnonymous()),
        directory=peer.directory(),
    )

    reply = await lifespan_startup(app)

    assert reply["type"] == "lifespan.startup.failed"
    assert "research-agent" in reply["message"]


# Asking


async def test_ask_sends_one_text_part_to_the_interface_url() -> None:
    peer = Peer(task(TaskState.TASK_STATE_COMPLETED, "Findings"))

    assert await ask(peer, "tides") == "Findings"

    sent = peer.requests[-1]
    body = json.loads(sent.content)
    assert str(sent.url) == A2A_URL
    assert sent.headers["A2A-Version"] == "1.0"
    assert body["params"]["message"]["parts"] == [{"text": "tides"}]
    assert body["params"]["configuration"]["returnImmediately"] is True
    # The card lists no context fields, so the message is plain A2A.
    assert "A2A-Extensions" not in sent.headers
    assert "metadata" not in body["params"]["message"]


def context_card(*fields: str) -> dict[str, Any]:
    extension = {"uri": SKILL_CALL_EXTENSION_URI, "params": {"context": list(fields)}}
    return card_json(capabilities={"extensions": [extension]})


async def test_ask_sends_the_context_fields_the_card_advertises() -> None:
    peer = Peer(
        task(TaskState.TASK_STATE_COMPLETED, "Findings"),
        card=context_card("deadline_ms", "hops"),
    )

    await ask(peer)

    sent = peer.requests[-1]
    metadata = json.loads(sent.content)["params"]["message"]["metadata"]
    assert sent.headers["A2A-Extensions"] == SKILL_CALL_EXTENSION_URI
    assert metadata["agentframe.hops"] == 1
    # A local run's default timeout is 120 seconds; what is left goes along.
    assert 119_000 < metadata["agentframe.deadline_ms"] <= 120_000


async def test_ask_sends_only_the_fields_the_card_advertises() -> None:
    peer = Peer(
        task(TaskState.TASK_STATE_COMPLETED, "Findings"), card=context_card("hops")
    )

    await ask(peer)

    body = json.loads(peer.requests[-1].content)
    assert body["params"]["message"]["metadata"] == {"agentframe.hops": 1}


async def test_a_run_at_the_depth_limit_fails_before_sending() -> None:
    peer = Peer(card=context_card("deadline_ms", "hops"))
    remote_agent = await research(peer, hops=5)

    with pytest.raises(LimitExceededError) as asked:
        await remote_agent.ask("tides")
    with pytest.raises(LimitExceededError) as called:
        await remote_agent.call("find_sources", topic="tides")

    assert asked.value.message == "Maximum remote call depth of 5 exceeded"
    assert asked.value.invocation_id == "run-1"
    assert called.value.message == asked.value.message
    assert peer.methods() == []


async def test_ask_polls_an_unfinished_task_until_it_completes() -> None:
    peer = Peer(
        task(TaskState.TASK_STATE_SUBMITTED),
        task(TaskState.TASK_STATE_WORKING),
        task(TaskState.TASK_STATE_COMPLETED, "Done"),
    )

    assert await ask(peer) == "Done"
    assert peer.methods() == ["SendMessage", "GetTask", "GetTask"]


def test_poll_delays_back_off_to_one_second() -> None:
    delays = REAL_POLL_DELAYS()
    assert list(itertools.islice(delays, 4)) == [0.25, 0.5, 1.0, 1.0]


async def test_a_task_that_disappears_raises_remote_agent_error_with_its_id() -> None:
    not_found = {"error": {"code": -32001, "message": "secret remote text"}}
    peer = Peer(task(TaskState.TASK_STATE_WORKING), not_found)

    with pytest.raises(RemoteAgentError) as raised:
        await ask(peer)

    assert raised.value.task_id == "task-1"
    assert "secret" not in raised.value.message


async def test_a_message_reply_returns_its_text() -> None:
    reply = Message(message_id="m", role=Role.ROLE_AGENT, parts=[new_text_part("Hi")])

    assert await ask(Peer(reply)) == "Hi"


async def test_a_non_text_result_raises_remote_agent_error() -> None:
    finished = task(TaskState.TASK_STATE_COMPLETED)
    finished.artifacts.add(parts=[new_data_part({"a": 1})])

    with pytest.raises(RemoteAgentError, match="did not answer with text") as raised:
        await ask(Peer(finished))

    assert raised.value.task_state == "TASK_STATE_COMPLETED"


# Calling a skill


async def research(peer: Peer, hops: int = 0) -> Research:
    deadline = asyncio.get_running_loop().time() + 10
    return await bind_remote(
        peer.directory(),
        "research-agent",
        deadline=deadline,
        invocation_id="run-1",
        principal=None,
        identity=None,
        hops=hops,
        max_hops=5,
        credential=None,
    )


async def test_call_sends_a_skill_call_with_json_arguments() -> None:
    peer = Peer(result([]), card=skill_call_card("find_sources"))

    await (await research(peer)).call("find_sources", topic=Topic(name="tides"))

    sent = peer.requests[-1]
    message = json.loads(sent.content)["params"]["message"]
    assert sent.headers["A2A-Extensions"] == SKILL_CALL_EXTENSION_URI
    assert sent.headers["A2A-Version"] == "1.0"
    assert message["metadata"] == {"agentframe.skill": "find_sources"}
    assert message["parts"] == [{"data": {"topic": {"name": "tides"}}}]


async def test_call_adds_the_advertised_context_under_the_same_extension() -> None:
    card = skill_call_card("find_sources")
    card["capabilities"]["extensions"][0]["params"] = {"context": ["hops"]}
    peer = Peer(result([]), card=card)

    await (await research(peer, hops=2)).call("find_sources", topic="tides")

    sent = peer.requests[-1]
    message = json.loads(sent.content)["params"]["message"]
    assert sent.headers["A2A-Extensions"] == SKILL_CALL_EXTENSION_URI
    assert message["metadata"] == {
        "agentframe.skill": "find_sources",
        "agentframe.hops": 3,
    }


async def test_call_validates_the_result_into_the_output_type() -> None:
    sources = [{"title": "Tide tables", "pages": 12}]
    peer = Peer(
        task(TaskState.TASK_STATE_WORKING),
        result(sources),
        card=skill_call_card("find_sources"),
    )

    found = await (await research(peer)).call(
        "find_sources", topic="tides", output=list[Source]
    )

    assert found == [Source(title="Tide tables", pages=12)]
    assert peer.methods() == ["SendMessage", "GetTask"]


async def test_call_without_output_returns_the_json_value_or_text() -> None:
    card = skill_call_card("count", "greet")
    text_result = task(TaskState.TASK_STATE_COMPLETED, "Hello")
    remote_agent = await research(Peer(result({"n": 3}), text_result, card=card))

    assert await remote_agent.call("count") == {"n": 3.0}
    assert await remote_agent.call("greet") == "Hello"


async def test_a_result_that_does_not_match_raises_output_validation_error() -> None:
    peer = Peer(result([{"title": "secret remote text"}]), card=skill_call_card("s"))

    with pytest.raises(OutputValidationError) as raised:
        await (await research(peer)).call("s", output=list[Source])

    assert "secret" not in raised.value.message
    assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    ("card", "missing"),
    [
        (card_json(), "skill-call extension"),
        (skill_call_card("other"), "no skill 'find_sources'"),
    ],
)
async def test_a_card_without_the_extension_or_skill_fails_before_sending(
    card: dict[str, Any], missing: str
) -> None:
    peer = Peer(card=card)

    with pytest.raises(RemoteAgentError, match=missing):
        await (await research(peer)).call("find_sources", topic="tides")

    assert peer.methods() == []


async def test_max_total_tokens_is_reserved_and_never_sent() -> None:
    peer = Peer(card=skill_call_card("s"))

    with pytest.raises(DefinitionError, match="reserved"):
        await (await research(peer)).call("s", max_total_tokens=100)

    assert peer.methods() == []


async def test_a_rejected_call_raises_remote_agent_error_with_the_state() -> None:
    rejected = task(TaskState.TASK_STATE_REJECTED)
    peer = Peer(rejected, card=skill_call_card("find_sources"))

    with pytest.raises(RemoteAgentError, match="did not complete") as raised:
        await (await research(peer)).call("find_sources", topic=3)

    assert raised.value.task_state == "TASK_STATE_REJECTED"


async def test_a_result_that_is_not_a_skill_result_raises_remote_agent_error() -> None:
    finished = task(TaskState.TASK_STATE_COMPLETED)
    finished.artifacts.add(parts=[new_data_part({"other": 1})])
    peer = Peer(finished, card=skill_call_card("s"))

    with pytest.raises(RemoteAgentError, match="did not answer with a skill result"):
        await (await research(peer)).call("s")


# Outbound identity


API_KEY_SCHEMES = {
    "apiKey": {"apiKeySecurityScheme": {"location": "header", "name": "X-API-Key"}}
}
OIDC_SCHEMES = {
    "entraId": {
        "openIdConnectSecurityScheme": {
            "openIdConnectUrl": "https://login.example.com/.well-known/openid"
        }
    }
}


def with_key(key: str = "planner-key") -> dict[str, Service]:
    return {"research-agent": Service(credential=APIKeyCredential(key))}


class FakeTokenCredential:
    """Stands in for an ``azure.core`` async token credential."""

    def __init__(self) -> None:
        self.scopes: list[tuple[str, ...]] = []

    async def get_token(self, *scopes: str) -> Any:
        self.scopes.append(scopes)
        return type("AccessToken", (), {"token": "app-token"})()


class FailingCredential:
    async def headers(self) -> Mapping[str, str]:
        raise RuntimeError("secret token endpoint detail")

    def satisfies(self, scheme: SecurityScheme) -> bool:
        return True


async def test_the_api_key_goes_with_every_request_but_never_the_card_fetch() -> None:
    peer = Peer(
        task(TaskState.TASK_STATE_WORKING),
        task(TaskState.TASK_STATE_COMPLETED, "Findings"),
        card=card_json(securitySchemes=API_KEY_SCHEMES),
    )
    runtime = Runtime(directory=peer.directory(), remotes=with_key())

    await runtime.invoke(Planner(), skill="plan", arguments={"goal": "tides"})

    card_fetch, *calls = peer.requests
    assert "X-API-Key" not in card_fetch.headers
    assert peer.methods() == ["SendMessage", "GetTask"]
    assert [r.headers["X-API-Key"] for r in calls] == ["planner-key"] * 2
    assert all(r.headers["A2A-Version"] == "1.0" for r in calls)


async def test_call_sends_the_credential_with_the_extension_header() -> None:
    card = skill_call_card("find_sources")
    card["securitySchemes"] = API_KEY_SCHEMES
    peer = Peer(result([]), card=card)
    remotes = with_key()
    binding = await bind_remote(
        peer.directory(),
        "research-agent",
        deadline=asyncio.get_running_loop().time() + 10,
        invocation_id="run-1",
        principal=None,
        identity=remotes["research-agent"],
        hops=0,
        max_hops=5,
        credential=remotes["research-agent"].credential,
    )

    await binding.call("find_sources", topic=Topic(name="tides"))

    sent = peer.requests[-1]
    assert sent.headers["X-API-Key"] == "planner-key"
    assert sent.headers["A2A-Extensions"] == SKILL_CALL_EXTENSION_URI


async def test_ask_sends_the_credential_and_context_under_one_extension() -> None:
    card = context_card("deadline_ms", "hops")
    card["securitySchemes"] = API_KEY_SCHEMES
    peer = Peer(task(TaskState.TASK_STATE_COMPLETED, "Findings"), card=card)
    binding = await bind_remote(
        peer.directory(),
        "research-agent",
        deadline=asyncio.get_running_loop().time() + 10,
        invocation_id="run-1",
        principal=None,
        identity=with_key()["research-agent"],
        credential=APIKeyCredential("planner-key"),
        hops=2,
        max_hops=5,
    )

    await binding.ask("tides")

    sent = peer.requests[-1]
    metadata = json.loads(sent.content)["params"]["message"]["metadata"]
    assert sent.headers["X-API-Key"] == "planner-key"
    assert sent.headers["A2A-Extensions"] == SKILL_CALL_EXTENSION_URI
    assert metadata["agentframe.hops"] == 3
    assert 9_000 < metadata["agentframe.deadline_ms"] <= 10_000


async def test_the_cancel_request_carries_the_credential_too() -> None:
    cancelled = task(TaskState.TASK_STATE_CANCELED)
    peer = Peer(
        task(TaskState.TASK_STATE_WORKING),
        "hang",
        cancelled,
        card=card_json(securitySchemes=API_KEY_SCHEMES),
    )
    runtime = Runtime(directory=peer.directory(), remotes=with_key())
    options = RunOptions(limits=RunLimits(timeout_seconds=0.2))

    with pytest.raises(TimeoutLimitExceededError):
        await runtime.invoke(
            Planner(), skill="plan", arguments={"goal": "x"}, options=options
        )

    assert peer.methods() == ["SendMessage", "GetTask", "CancelTask"]
    assert peer.requests[-1].headers["X-API-Key"] == "planner-key"


async def test_an_entra_app_credential_sends_its_token_as_a_bearer() -> None:
    credential = FakeTokenCredential()
    peer = Peer(
        task(TaskState.TASK_STATE_COMPLETED, "Findings"),
        card=card_json(securitySchemes=OIDC_SCHEMES),
    )
    entra = EntraAppCredential(credential, scope="api://research/.default")
    remotes = {"research-agent": Service(credential=entra)}
    runtime = Runtime(directory=peer.directory(), remotes=remotes)

    await runtime.invoke(Planner(), skill="plan", arguments={"goal": "tides"})

    assert peer.requests[-1].headers["Authorization"] == "Bearer app-token"
    assert credential.scopes == [("api://research/.default",)]
    assert "Authorization" not in peer.requests[0].headers


async def test_a_card_requiring_credentials_without_an_identity_names_the_remote() -> (
    None
):
    peer = Peer(card=card_json(securitySchemes=API_KEY_SCHEMES))

    with pytest.raises(DefinitionError, match="'research-agent' requires credentials"):
        await Runtime(directory=peer.directory()).validate_remotes(Planner())


async def test_a_mismatched_credential_fails_startup_without_the_secret() -> None:
    peer = Peer(card=card_json(securitySchemes=OIDC_SCHEMES))
    app = AgentApp(
        agent=Planner(),
        host=HostConfig(public_url="http://127.0.0.1:8000", auth=AllowAnonymous()),
        directory=peer.directory(),
        remotes=with_key("super-secret-key"),
    )

    reply = await lifespan_startup(app)

    assert reply["type"] == "lifespan.startup.failed"
    assert "'research-agent'" in reply["message"]
    assert "APIKeyCredential cannot provide" in reply["message"]
    assert "super-secret-key" not in reply["message"]


async def test_credentials_never_go_to_an_interface_on_another_origin() -> None:
    card = card_json(
        "https://elsewhere.example.com/a2a", securitySchemes=API_KEY_SCHEMES
    )
    peer = Peer(card=card)
    runtime = Runtime(directory=peer.directory(), remotes=with_key())

    with pytest.raises(RemoteAgentError, match="not on the card URL's origin"):
        await runtime.invoke(Planner(), skill="plan", arguments={"goal": "x"})

    assert [str(r.url) for r in peer.requests] == [CARD_URL]
    assert "X-API-Key" not in peer.requests[0].headers


async def test_a_redirect_is_not_followed_with_credentials() -> None:
    redirect = httpx.Response(307, headers={"Location": "https://evil.example.com/"})
    peer = Peer(card=card_json(securitySchemes=API_KEY_SCHEMES))

    async def handle(request: httpx.Request) -> httpx.Response:
        peer.requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=peer.card)
        return redirect

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    directory = AgentDirectory(sources=[OneAgent()], http_client=client)
    runtime = Runtime(directory=directory, remotes=with_key())

    with pytest.raises(RemoteAgentError, match="could not be reached"):
        await runtime.invoke(Planner(), skill="plan", arguments={"goal": "x"})

    assert [r.url.host for r in peer.requests] == ["research.example.com"] * 2


async def test_credentials_are_refused_with_a_client_that_follows_redirects() -> None:
    peer = Peer(card=card_json(securitySchemes=API_KEY_SCHEMES))
    runtime = Runtime(
        directory=peer.directory(follow_redirects=True), remotes=with_key()
    )

    with pytest.raises(DefinitionError, match="follows redirects"):
        await runtime.validate_remotes(Planner())


async def test_a_failing_credential_raises_a_safe_authentication_error() -> None:
    peer = Peer(card=card_json(securitySchemes=API_KEY_SCHEMES))
    remotes = {"research-agent": Service(credential=FailingCredential())}
    runtime = Runtime(directory=peer.directory(), remotes=remotes)

    with pytest.raises(AuthenticationError) as raised:
        await runtime.invoke(Planner(), skill="plan", arguments={"goal": "x"})

    assert "secret" not in raised.value.message
    assert raised.value.__cause__ is None
    assert peer.methods() == []


async def test_an_open_card_is_called_with_the_configured_credential() -> None:
    peer = Peer(task(TaskState.TASK_STATE_COMPLETED, "Findings"))
    runtime = Runtime(directory=peer.directory(), remotes=with_key())

    assert await runtime.invoke(Planner(), skill="plan", arguments={"goal": "x"})
    assert peer.requests[-1].headers["X-API-Key"] == "planner-key"


def test_remotes_must_map_names_to_service_identities() -> None:
    with pytest.raises(DefinitionError, match="Service"):
        Runtime(remotes={"research-agent": APIKeyCredential("k")})  # type: ignore[dict-item]


def test_on_behalf_of_needs_a_scope() -> None:
    with pytest.raises(DefinitionError, match="scope"):
        OnBehalfOf(scope="")


def test_remotes_cannot_be_combined_with_a_runtime_on_agent_app() -> None:
    host = HostConfig(public_url="http://127.0.0.1:8000", auth=AllowAnonymous())

    with pytest.raises(DefinitionError, match="not both"):
        AgentApp(
            agent=Planner(),
            host=host,
            runtime=Runtime(directory=Peer().directory()),
            remotes=with_key(),
        )
