"""Tests for per-caller rate limiting: ``HostConfig.rate_limit``.

The limiter is tested with a fake clock; the host's use of it over real
localhost HTTP to an ``AgentApp`` served by Uvicorn. Hosts here allow each
caller two messages an hour, so no allowance refills during a test.
"""

import math
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
import pytest

from agentframe import (
    Agent,
    AgentApp,
    AllowAnonymous,
    APIKey,
    APIKeyAuth,
    Authenticator,
    DefinitionError,
    HostConfig,
    RateLimit,
    RunContext,
    agent,
    skill,
)
from agentframe.hosting.card import SKILL_CALL_EXTENSION_URI
from agentframe.hosting.rate_limit import CallerRateLimiter
from local_server import ServeLocally

# Test-only values; real applications load keys from the environment.
ALICE_KEY = "alice-test-key"
BOB_KEY = "bob-test-key"
KEYS = APIKeyAuth(keys={ALICE_KEY: APIKey("alice"), BOB_KEY: APIKey("bob")})

TWO_AN_HOUR = RateLimit(requests=2, per_seconds=3600)


class FakeClock:
    """A clock the test moves forward by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


# The limiter


def test_a_caller_may_burst_up_to_the_allowance() -> None:
    limiter = CallerRateLimiter(RateLimit(requests=3, per_seconds=60), FakeClock())

    assert [limiter.try_acquire("alice") for _ in range(3)] == [None, None, None]
    assert limiter.try_acquire("alice") == pytest.approx(20)


def test_the_allowance_refills_at_the_steady_rate() -> None:
    clock = FakeClock()
    limiter = CallerRateLimiter(RateLimit(requests=2, per_seconds=10), clock)
    limiter.try_acquire("alice")
    limiter.try_acquire("alice")

    clock.now = 2.0
    assert limiter.try_acquire("alice") == pytest.approx(3)
    clock.now = 5.0
    assert limiter.try_acquire("alice") is None
    assert limiter.try_acquire("alice") == pytest.approx(5)


def test_callers_have_separate_allowances_and_anonymous_callers_share_one() -> None:
    limiter = CallerRateLimiter(RateLimit(requests=1, per_seconds=60), FakeClock())

    assert limiter.try_acquire("alice") is None
    assert limiter.try_acquire("bob") is None
    assert limiter.try_acquire(None) is None
    assert limiter.try_acquire(None) is not None
    assert limiter.try_acquire("alice") is not None


def test_a_caller_idle_for_a_period_is_forgotten() -> None:
    clock = FakeClock()
    limiter = CallerRateLimiter(RateLimit(requests=2, per_seconds=10), clock)
    limiter.try_acquire("alice")

    clock.now = 10.0
    limiter.try_acquire("bob")

    assert list(limiter._buckets) == ["bob"]


@pytest.mark.parametrize(
    "fields",
    [
        {"requests": 0},
        {"requests": 1, "per_seconds": 0},
        {"requests": 1, "per_seconds": math.inf},
    ],
)
def test_the_rate_limit_must_be_positive(fields: dict[str, Any]) -> None:
    with pytest.raises(DefinitionError):
        RateLimit(**fields)


# The host


@agent(version="1.0.0")
class Counter(Agent):
    """Counts how often its skill really runs."""

    def __init__(self) -> None:
        self.runs = 0

    @skill("count", tags=["test"])
    async def count(self, ctx: RunContext) -> str:
        """Count one run."""
        self.runs += 1
        return f"run {self.runs}"


@pytest.fixture
def counter() -> Counter:
    return Counter()


def make_app(
    counter: Counter, public_url: str, *, auth: Authenticator = KEYS
) -> AgentApp:
    host = HostConfig(public_url=public_url, auth=auth, rate_limit=TWO_AN_HOUR)
    return AgentApp(agent=counter, host=host)


@pytest.fixture
async def http(
    serve: ServeLocally, counter: Counter
) -> AsyncIterator[httpx.AsyncClient]:
    async with (
        serve(lambda url: make_app(counter, url)) as url,
        httpx.AsyncClient(base_url=url) as client,
    ):
        yield client


async def post(
    http: httpx.AsyncClient, method: str, params: Mapping[str, Any], key: str | None
) -> httpx.Response:
    headers = {"A2A-Version": "1.0", "A2A-Extensions": SKILL_CALL_EXTENSION_URI}
    if key is not None:
        headers["X-API-Key"] = key
    body = {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}
    return await http.post("/a2a", json=body, headers=headers)


def count_message(message_id: str | None = None) -> dict[str, Any]:
    return {
        "messageId": message_id or str(uuid.uuid4()),
        "role": "ROLE_USER",
        "parts": [{"data": {}}],
        "metadata": {"agentframe.skill": "count"},
    }


async def send(
    http: httpx.AsyncClient, key: str | None = ALICE_KEY, message_id: str | None = None
) -> httpx.Response:
    return await post(http, "SendMessage", {"message": count_message(message_id)}, key)


async def test_a_message_beyond_the_allowance_gets_429_and_creates_no_task(
    http: httpx.AsyncClient, counter: Counter
) -> None:
    await send(http)
    await send(http)

    refused = await send(http)

    assert refused.status_code == 429
    assert refused.json() == {"error": "Too many requests; retry later."}
    assert refused.headers["Retry-After"] == "1800"
    assert counter.runs == 2
    listed = await post(http, "ListTasks", {}, ALICE_KEY)
    assert len(listed.json()["result"]["tasks"]) == 2


async def test_task_reads_are_not_limited(http: httpx.AsyncClient) -> None:
    task_id = (await send(http)).json()["result"]["task"]["id"]
    await send(http)

    for _ in range(3):
        read = await post(http, "GetTask", {"id": task_id}, ALICE_KEY)
        assert read.json()["result"]["id"] == task_id


async def test_each_caller_has_its_own_allowance(http: httpx.AsyncClient) -> None:
    await send(http, ALICE_KEY)
    await send(http, ALICE_KEY)

    assert (await send(http, BOB_KEY)).status_code == 200
    assert (await send(http, ALICE_KEY)).status_code == 429


async def test_a_repeated_message_counts_against_the_allowance(
    http: httpx.AsyncClient, counter: Counter
) -> None:
    await send(http, message_id="message-1")
    await send(http, message_id="message-1")

    assert (await send(http, message_id="message-1")).status_code == 429
    assert counter.runs == 1


async def test_an_unauthenticated_request_uses_no_allowance(
    http: httpx.AsyncClient,
) -> None:
    for _ in range(3):
        assert (await send(http, "wrong-key")).status_code == 401

    assert (await send(http)).status_code == 200


async def test_anonymous_callers_share_one_allowance(
    serve: ServeLocally, counter: Counter
) -> None:
    async with (
        serve(lambda url: make_app(counter, url, auth=AllowAnonymous())) as url,
        httpx.AsyncClient(base_url=url) as http,
    ):
        statuses = [(await send(http, key=None)).status_code for _ in range(3)]

    assert statuses == [200, 200, 429]
