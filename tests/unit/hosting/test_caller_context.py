"""Tests for the caller context an A2A request carries: deadline, depth, tokens
and correlation.

Requests travel over real localhost HTTP to an ``AgentApp`` served by Uvicorn.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx
import pytest
from a2a.helpers import get_message_text
from a2a.types import AgentCard, AgentExtension, Message

from agentframe import (
    Agent,
    AgentApp,
    AllowAnonymous,
    HostConfig,
    RoutingContext,
    RunContext,
    RunLimits,
    SkillCall,
    SkillDefinition,
    SkillRouter,
    agent,
    skill,
)
from agentframe.hosting.skill_call import (
    SKILL_CALL_EXTENSION_URI,
    advertised_context,
    context_metadata,
)
from local_server import ServeLocally

INVALID_PARAMS = -32602
ROOT = "6f1c2d3e-4a5b-4c6d-8e7f-901a2b3c4d5e"
PARENT = "0a1b2c3d-4e5f-4a6b-9c7d-8e9f0a1b2c3d"


@agent(version="1.0.0")
class Sleeper(Agent):
    """Sleeps, and records the depth and time left of every run."""

    limits = RunLimits(max_remote_hops=3)

    def __init__(self) -> None:
        self.runs: list[tuple[int, float]] = []

    @skill("nap", tags=["test"], limits=RunLimits(timeout_seconds=0.5))
    async def nap(self, seconds: float, ctx: RunContext) -> str:
        """Sleep for a while."""
        time_left = ctx.deadline - asyncio.get_running_loop().time()
        self.runs.append((ctx.hops, time_left))
        await asyncio.sleep(seconds)
        return "rested"


class NapRouter(SkillRouter):
    """Routes any text, as a number of seconds, to ``nap``."""

    def __init__(self) -> None:
        self.routed = 0

    async def route(
        self,
        conversation: Sequence[Message],
        skills: Sequence[SkillDefinition],
        ctx: RoutingContext,
    ) -> SkillCall:
        self.routed += 1
        return SkillCall("nap", {"seconds": float(get_message_text(conversation[-1]))})


@pytest.fixture
def sleeper() -> Sleeper:
    return Sleeper()


@pytest.fixture
def router() -> NapRouter:
    return NapRouter()


@pytest.fixture
async def http(
    serve: ServeLocally, sleeper: Sleeper, router: NapRouter
) -> AsyncIterator[httpx.AsyncClient]:
    def make_app(public_url: str) -> AgentApp:
        return AgentApp(
            agent=sleeper,
            host=HostConfig(public_url=public_url, auth=AllowAnonymous()),
            router=router,
        )

    async with serve(make_app) as base_url, httpx.AsyncClient(base_url=base_url) as h:
        yield h


async def send(
    http: httpx.AsyncClient,
    metadata: dict[str, Any],
    *,
    text: str = "0",
    activate: bool = True,
) -> dict[str, Any]:
    """Send free text with ``metadata``; return the JSON-RPC response."""
    message = {
        "messageId": str(uuid.uuid4()),
        "role": "ROLE_USER",
        "parts": [{"text": text}],
        "metadata": metadata,
    }
    body = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {"message": message},
    }
    headers = {"A2A-Version": "1.0"}
    if activate:
        headers["A2A-Extensions"] = SKILL_CALL_EXTENSION_URI
    response = await http.post("/a2a", json=body, headers=headers)
    result: dict[str, Any] = response.json()
    return result


def state(response: dict[str, Any]) -> str:
    value: str = response["result"]["task"]["status"]["state"]
    return value


def status_text(response: dict[str, Any]) -> str:
    parts = response["result"]["task"]["status"]["message"]["parts"]
    return " ".join(part["text"] for part in parts)


# Receiving


async def test_an_activated_message_without_a_skill_is_routed_with_its_context(
    http: httpx.AsyncClient, sleeper: Sleeper
) -> None:
    response = await send(http, {"agentframe.hops": 2, "agentframe.deadline_ms": 300})

    assert state(response) == "TASK_STATE_COMPLETED"
    [(hops, time_left)] = sleeper.runs
    assert hops == 2
    assert 0 < time_left <= 0.3


async def test_a_skill_call_carries_context_too(
    http: httpx.AsyncClient, sleeper: Sleeper
) -> None:
    message = {
        "messageId": str(uuid.uuid4()),
        "role": "ROLE_USER",
        "parts": [{"data": {"seconds": 0}}],
        "metadata": {"agentframe.skill": "nap", "agentframe.hops": 1},
    }
    body = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {"message": message},
    }
    headers = {"A2A-Version": "1.0", "A2A-Extensions": SKILL_CALL_EXTENSION_URI}

    response = (await http.post("/a2a", json=body, headers=headers)).json()

    assert state(response) == "TASK_STATE_COMPLETED"
    assert sleeper.runs[0][0] == 1


async def test_without_context_a_request_is_a_root_with_its_own_timeout(
    http: httpx.AsyncClient, sleeper: Sleeper
) -> None:
    response = await send(http, {})

    assert state(response) == "TASK_STATE_COMPLETED"
    [(hops, time_left)] = sleeper.runs
    assert hops == 0
    assert 0.4 < time_left <= 0.5


async def test_a_shorter_caller_deadline_cuts_the_run_short(
    http: httpx.AsyncClient,
) -> None:
    started = asyncio.get_running_loop().time()

    response = await send(http, {"agentframe.deadline_ms": 100}, text="5")

    assert state(response) == "TASK_STATE_FAILED"
    assert "timeout" in status_text(response)
    assert asyncio.get_running_loop().time() - started < 0.4


async def test_a_longer_caller_deadline_does_not_extend_the_run(
    http: httpx.AsyncClient,
) -> None:
    started = asyncio.get_running_loop().time()

    response = await send(http, {"agentframe.deadline_ms": 60_000}, text="5")

    assert state(response) == "TASK_STATE_FAILED"
    assert "timeout of 0.5 seconds" in status_text(response)
    assert asyncio.get_running_loop().time() - started < 2


async def test_a_request_deeper_than_the_limit_is_rejected_before_routing(
    http: httpx.AsyncClient, sleeper: Sleeper, router: NapRouter
) -> None:
    response = await send(http, {"agentframe.hops": 4})

    assert state(response) == "TASK_STATE_REJECTED"
    assert status_text(response) == "Maximum remote call depth of 3 exceeded"
    assert response["result"]["task"]["metadata"]["invocation_id"]
    assert router.routed == 0
    assert sleeper.runs == []


async def test_a_request_at_the_limit_runs(
    http: httpx.AsyncClient, sleeper: Sleeper
) -> None:
    response = await send(http, {"agentframe.hops": 3})

    assert state(response) == "TASK_STATE_COMPLETED"
    assert sleeper.runs[0][0] == 3


async def test_context_is_ignored_unless_the_extension_is_activated(
    http: httpx.AsyncClient, sleeper: Sleeper
) -> None:
    response = await send(
        http, {"agentframe.hops": 99, "agentframe.deadline_ms": "x"}, activate=False
    )

    assert state(response) == "TASK_STATE_COMPLETED"
    assert sleeper.runs[0][0] == 0


@pytest.mark.parametrize(
    ("metadata", "problem"),
    [
        pytest.param({"agentframe.hops": "1"}, "agentframe.hops", id="hops-string"),
        pytest.param({"agentframe.hops": -1}, "agentframe.hops", id="hops-negative"),
        pytest.param({"agentframe.hops": 1.5}, "agentframe.hops", id="hops-fraction"),
        pytest.param({"agentframe.hops": True}, "agentframe.hops", id="hops-bool"),
        pytest.param(
            {"agentframe.deadline_ms": 0}, "agentframe.deadline_ms", id="deadline-zero"
        ),
        pytest.param(
            {"agentframe.deadline_ms": None},
            "agentframe.deadline_ms",
            id="deadline-null",
        ),
        pytest.param(
            {"agentframe.max_total_tokens": 0},
            "agentframe.max_total_tokens",
            id="tokens-zero",
        ),
        pytest.param(
            {"agentframe.max_total_tokens": 10.5},
            "agentframe.max_total_tokens",
            id="tokens-fraction",
        ),
        pytest.param({"agentframe.budget": 1}, "agentframe.budget", id="unknown-field"),
    ],
)
async def test_malformed_context_is_a_protocol_error_without_a_task(
    http: httpx.AsyncClient, metadata: dict[str, Any], problem: str
) -> None:
    response = await send(http, metadata)

    assert response["error"]["code"] == INVALID_PARAMS
    assert problem in response["error"]["message"]


# Receiving correlation: only labels the run's logs


def run_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith("Run ")]


async def test_a_valid_root_and_parent_label_every_log_record_of_the_run(
    http: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="agentframe")
    metadata = {
        "agentframe.root_invocation_id": ROOT,
        "agentframe.parent_invocation_id": PARENT,
    }

    response = await send(http, metadata)

    invocation_id = response["result"]["task"]["metadata"]["invocation_id"]
    records = run_records(caplog)
    assert [r.getMessage().split(" (")[0] for r in records] == [
        "Run started",
        "Run completed",
    ]
    for record in records:
        assert record.__dict__["agent"] == "Sleeper"
        assert record.__dict__["invocation_id"] == invocation_id
        assert record.__dict__["root_invocation_id"] == ROOT
        assert record.__dict__["parent_invocation_id"] == PARENT


@pytest.mark.parametrize(
    "root",
    [
        pytest.param("not-a-uuid\nERROR forged log line", id="newline"),
        pytest.param(ROOT.upper(), id="uppercase"),
        pytest.param("{" + ROOT + "}", id="braces"),
        pytest.param(ROOT + "x" * 1000, id="too-long"),
        pytest.param(12345, id="number"),
        pytest.param(None, id="null"),
    ],
)
async def test_an_invalid_root_is_ignored_and_the_run_is_its_own_root(
    http: httpx.AsyncClient, caplog: pytest.LogCaptureFixture, root: object
) -> None:
    caplog.set_level(logging.DEBUG, logger="agentframe")
    metadata = {
        "agentframe.root_invocation_id": root,
        "agentframe.parent_invocation_id": PARENT,
    }

    response = await send(http, metadata)

    assert state(response) == "TASK_STATE_COMPLETED"
    task_metadata = response["result"]["task"]["metadata"]
    assert task_metadata.keys() == {"invocation_id", "usage"}
    for record in run_records(caplog):
        assert record.__dict__["root_invocation_id"] == task_metadata["invocation_id"]
        # A parent without a valid root is dropped too.
        assert record.__dict__["parent_invocation_id"] is None
    # Never echoed into any log record.
    assert str(root) not in caplog.text
    assert PARENT not in caplog.text


async def test_an_invalid_parent_is_ignored_but_a_valid_root_kept(
    http: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="agentframe")
    metadata = {
        "agentframe.root_invocation_id": ROOT,
        "agentframe.parent_invocation_id": "evil\r\nparent",
    }

    response = await send(http, metadata)

    assert state(response) == "TASK_STATE_COMPLETED"
    [started, _] = run_records(caplog)
    assert started.__dict__["root_invocation_id"] == ROOT
    assert started.__dict__["parent_invocation_id"] is None


async def test_correlation_is_ignored_unless_the_extension_is_activated(
    http: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="agentframe")

    response = await send(http, {"agentframe.root_invocation_id": ROOT}, activate=False)

    invocation_id = response["result"]["task"]["metadata"]["invocation_id"]
    [started, _] = run_records(caplog)
    assert started.__dict__["root_invocation_id"] == invocation_id


# Sending: what a card advertises decides what is sent


def card_with(*extensions: AgentExtension) -> AgentCard:
    card = AgentCard(name="Peer")
    card.capabilities.extensions.extend(extensions)
    return card


def test_a_card_lists_its_context_fields_in_the_extension_params() -> None:
    card = card_with(
        AgentExtension(
            uri=SKILL_CALL_EXTENSION_URI,
            params={"context": ["deadline_ms", "hops", "future_field"]},
        )
    )

    assert advertised_context(card) == {"deadline_ms", "hops"}


@pytest.mark.parametrize(
    "card",
    [
        pytest.param(card_with(), id="no-extension"),
        pytest.param(
            card_with(AgentExtension(uri=SKILL_CALL_EXTENSION_URI, params={})),
            id="0.1.0-extension-without-context",
        ),
        pytest.param(
            card_with(
                AgentExtension(uri=SKILL_CALL_EXTENSION_URI, params={"context": "hops"})
            ),
            id="not-a-list",
        ),
        pytest.param(
            card_with(
                AgentExtension(uri="https://example.com/other", params={"context": []})
            ),
            id="other-extension",
        ),
    ],
)
def test_a_card_without_context_fields_advertises_none(card: AgentCard) -> None:
    assert advertised_context(card) == frozenset()


def sent(fields: set[str], *, remaining_seconds: float = 2, hops: int = 3) -> Any:
    return context_metadata(
        fields,
        remaining_seconds=remaining_seconds,
        hops=hops,
        root_invocation_id=ROOT,
        parent_invocation_id=PARENT,
    )


def test_only_advertised_fields_are_sent() -> None:
    assert sent({"hops"}) == {"agentframe.hops": 3}
    assert sent(set()) == {}
    assert sent({"root_invocation_id", "parent_invocation_id"}) == {
        "agentframe.root_invocation_id": ROOT,
        "agentframe.parent_invocation_id": PARENT,
    }


def test_a_token_ceiling_is_sent_only_when_advertised_and_given() -> None:
    fields = {"max_total_tokens"}

    sent = context_metadata(fields, remaining_seconds=2, hops=1, max_total_tokens=500)
    none = context_metadata(fields, remaining_seconds=2, hops=1)
    unlisted = context_metadata(set(), remaining_seconds=2, hops=1, max_total_tokens=5)

    assert sent == {"agentframe.max_total_tokens": 500}
    assert none == unlisted == {}


def test_the_deadline_is_sent_as_whole_milliseconds_rounded_down() -> None:
    assert sent({"deadline_ms"}, remaining_seconds=1.9999) == {
        "agentframe.deadline_ms": 1999
    }
    assert sent({"deadline_ms"}, remaining_seconds=0.0001) == {
        "agentframe.deadline_ms": 1
    }
