"""M7 acceptance: the first release's hardening guarantees, end to end.

Each section proves one group of guarantees the way an application sees them:
local runs through ``Runtime`` and hosted runs through the official A2A SDK
client, over real localhost HTTP. Shutdown is driven in-process, as an ASGI
server would drive it, so a test can watch runs being cancelled. Nothing
leaves localhost and no model provider is called.

1. Limits: library, agent, skill and run defaults, capped by a host ceiling.
2. Deadlines: policy checks, service setup, routing, model calls and retry
   waits all spend from one deadline.
3. Cancellation: local runs re-raise ``asyncio.CancelledError`` unchanged;
   hosted cancels and shutdown end tasks ``CANCELED``; services are cleaned up.
4. Services: missing ones fail startup; per-run ones close however a run ends.
5. ``RunResult`` reports execution metadata without any tracing configured.
6. Safe, correlated errors, and logs without arguments, prompts or keys.
7. Identity, resource and policy failures, including Microsoft Entra tokens.
8. Host protection: admission, byte limits, probes, drain, deduplication and
   caller isolation.
"""

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from a2a.client import A2AClientError, Client
from a2a.helpers import (
    get_artifact_text,
    get_data_parts,
    get_message_text,
    new_data_part,
)
from a2a.types import (
    CancelTaskRequest,
    GetTaskRequest,
    ListTasksRequest,
    Message,
    Role,
    Task,
    TaskState,
)
from a2a.utils.errors import InvalidParamsError, TaskNotFoundError
from pydantic import BaseModel

from a2a_client import call_skill, connect, say, send
from agentframe import (
    Agent,
    AgentApp,
    APIKey,
    APIKeyAuth,
    Authenticator,
    Clarification,
    DefinitionError,
    EntraIDAuth,
    ExecutionPolicy,
    FinishReason,
    HostConfig,
    Inject,
    ModelCallLimitExceededError,
    ModelRequest,
    ModelResponse,
    Principal,
    ProviderError,
    RoutingContext,
    RunContext,
    RunLimits,
    RunOptions,
    Runtime,
    Services,
    SkillCall,
    SkillDefinition,
    SkillRouter,
    TimeoutLimitExceededError,
    Usage,
    agent,
    skill,
)
from agentframe.testing import ScriptedModel
from fake_entra import API_CLIENT_ID, TENANT_ID, FakeEntra, new_rsa_key, sign
from local_server import Lifespan, ServeLocally

# Test-only values; real applications load keys from the environment.
ALICE_KEY = "alice-test-key"
BOB_KEY = "bob-test-key"
API_KEYS = {ALICE_KEY: APIKey("alice"), BOB_KEY: APIKey("bob", roles=["Teller"])}
ROLE_PERMISSIONS = {"Teller": ["payments:write"], "Editor": ["payments:write"]}

# Values that must never reach a caller or a log.
SECRET_ACCOUNT = "acct-4111-SECRET"
SECRET_PROMPT = "PROMPT-SECRET-c0ffee"
SECRET_KEY = "key-SECRET-9a8b7c"

FAILED = TaskState.TASK_STATE_FAILED
REJECTED = TaskState.TASK_STATE_REJECTED


# Shared building blocks


class Connection:
    """A per-run connection that records whether it was closed."""

    def __init__(self) -> None:
        self.closed = False


class Connections:
    """Opens one ``Connection`` per run and remembers every one it opened."""

    def __init__(self) -> None:
        self.opened: list[Connection] = []

    async def open(self) -> AsyncIterator[Connection]:
        connection = Connection()
        self.opened.append(connection)
        yield connection
        connection.closed = True

    def services(self) -> Services:
        services = Services()
        services.add_scoped(Connection, self.open)
        return services


class SlowModel(ScriptedModel):
    """A scripted model that takes ``seconds`` to answer each call."""

    def __init__(self, replies: Sequence[str], *, seconds: float) -> None:
        super().__init__(replies)
        self.seconds = seconds

    async def generate(self, request: ModelRequest) -> ModelResponse:
        await asyncio.sleep(self.seconds)
        return await super().generate(request)


class SlowAuthorization:
    """A resource policy that takes ``seconds`` to allow every run."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    async def check(
        self,
        principal: Principal | None,
        skill: SkillDefinition,
        arguments: Mapping[str, object],
    ) -> bool:
        await asyncio.sleep(self.seconds)
        return True


async def eventually(condition: Callable[[], bool]) -> None:
    """Wait up to five seconds for ``condition`` to become true."""
    async with asyncio.timeout(5):
        while not condition():
            await asyncio.sleep(0.01)


def elapsed_since(started: float) -> float:
    return time.monotonic() - started


# The hosted application: a bank desk with per-run services and policies


class LedgerPolicy:
    """Resource policy backed by a ledger service that can go down."""

    def __init__(self) -> None:
        self.available = True

    async def check(
        self,
        principal: Principal | None,
        skill: SkillDefinition,
        arguments: Mapping[str, object],
    ) -> bool:
        if not self.available:
            # The message holds the arguments, so it must reach no caller or log.
            raise ConnectionError(f"Ledger unreachable for {arguments}")
        return arguments.get("account") != "frozen"


async def transfer_limit(
    skill: SkillDefinition, principal: Principal | None, arguments: Mapping[str, object]
) -> bool:
    """Execution policy input check: no transfer may exceed 1000."""
    amount = arguments.get("amount", 0)
    return isinstance(amount, int) and amount <= 1000


@agent(version="1.0.0")
class Desk(Agent):
    """A bank desk."""

    def __init__(self, *, model: ScriptedModel) -> None:
        super().__init__(model=model)
        self.waiting = 0
        self.release = asyncio.Event()
        self.echoes = 0

    @skill("echo", tags=["test"])
    async def echo(self, text: str, ctx: RunContext) -> str:
        """Repeat the text."""
        self.echoes += 1
        return text

    @skill("balance", tags=["accounts"])
    async def balance(self, account: str, ctx: RunContext) -> int:
        """Return an account's balance."""
        return 100

    @skill("transfer", tags=["payments"], requires_permissions=["payments:write"])
    async def transfer(self, account: str, amount: int, ctx: RunContext) -> str:
        """Send money from an account."""
        return f"Sent {amount}."

    @skill("statement", tags=["accounts"])
    async def statement(self, account: str, ctx: RunContext) -> str:
        """Print a statement; the ledger is always down."""
        # Skill authors keep secrets out of exception messages: the traceback
        # is logged for operators.
        raise RuntimeError("The ledger is unavailable.")

    @skill("advise", tags=["advice"])
    async def advise(self, question: str, ctx: RunContext) -> str:
        """Answer a question with the model."""
        return await ctx.generate(question)

    @skill("nap", tags=["test"], limits=RunLimits(timeout_seconds=0.5))
    async def nap(self, seconds: float, ctx: RunContext) -> str:
        """Sleep, within a half-second timeout."""
        await asyncio.sleep(seconds)
        return "Rested."

    @skill("wait", tags=["test"])
    async def wait(self, ctx: RunContext, connection: Inject[Connection]) -> str:
        """Hold a per-run connection until the test releases the desk."""
        self.waiting += 1
        await self.release.wait()
        return "Done."


class Bank:
    """The application under test: the desk, its services and its policies."""

    def __init__(self) -> None:
        failing_model = ScriptedModel(
            [ProviderError("The model provider failed.", transient=False)]
        )
        self.desk = Desk(model=failing_model)
        self.connections = Connections()
        self.ledger = LedgerPolicy()

    def app(
        self,
        public_url: str,
        *,
        auth: Authenticator | None = None,
        router: SkillRouter | None = None,
        **host_options: Any,
    ) -> AgentApp:
        return AgentApp(
            agent=self.desk,
            host=HostConfig(
                public_url=public_url,
                auth=auth or APIKeyAuth(keys=API_KEYS),
                role_permissions=ROLE_PERMISSIONS,
                **host_options,
            ),
            services=self.connections.services(),
            authorization=self.ledger,
            policy=ExecutionPolicy(input_checks=[transfer_limit]),
            router=router,
        )


@pytest.fixture
def bank() -> Bank:
    return Bank()


@pytest.fixture
async def base_url(serve: ServeLocally, bank: Bank) -> AsyncIterator[str]:
    async with serve(bank.app) as url:
        yield url


@pytest.fixture
async def alice(base_url: str) -> AsyncIterator[Client]:
    async with await connect(base_url, ALICE_KEY) as client:
        yield client


@pytest.fixture
async def bob(base_url: str) -> AsyncIterator[Client]:
    async with await connect(base_url, BOB_KEY) as client:
        yield client


IN_PROCESS_URL = "http://desk.test"


@asynccontextmanager
async def in_process(app: AgentApp) -> AsyncIterator[tuple[Lifespan, Client]]:
    """Start ``app`` as an ASGI server would and connect Alice to it in-process."""
    lifespan = Lifespan(app)
    assert await lifespan.startup() == "lifespan.startup.complete"
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport) as http,
        await connect(IN_PROCESS_URL, ALICE_KEY, http=http) as client,
    ):
        yield lifespan, client


async def get_task(client: Client, task_id: str) -> Task:
    return await client.get_task(GetTaskRequest(id=task_id))


async def start_waiting(client: Client, desk: Desk) -> Task:
    """Start a non-blocking ``wait`` run and return once the skill is running."""
    already_waiting = desk.waiting
    task = await call_skill(client, "wait", return_immediately=True)
    await eventually(lambda: desk.waiting > already_waiting)
    return task


UNFINISHED = (TaskState.TASK_STATE_SUBMITTED, TaskState.TASK_STATE_WORKING)


async def wait_until_finished(client: Client, task_id: str) -> Task:
    async with asyncio.timeout(5):
        while True:
            task = await get_task(client, task_id)
            if task.status.state not in UNFINISHED:
                return task
            await asyncio.sleep(0.01)


def value_of(task: Task) -> object:
    [data] = get_data_parts(task.artifacts[0].parts)
    return data["value"]


def status_text(task: Task) -> str:
    return get_message_text(task.status.message)


def agent_reply(task: Task) -> str:
    """Everything the agent sent back: status, artifacts and metadata.

    The task's history also holds the caller's own message, which is left out.
    """
    return f"{task.status} {list(task.artifacts)} {task.metadata}"


def invocation_id(task: Task) -> str:
    value = task.metadata["invocation_id"]
    assert isinstance(value, str)
    return str(uuid.UUID(value))


def http_error(raised: pytest.ExceptionInfo[A2AClientError]) -> httpx.Response:
    cause = raised.value.__cause__
    assert isinstance(cause, httpx.HTTPStatusError)
    return cause.response


# 1. Limits: defaults and precedence within ceilings


class Budget(BaseModel):
    """What a run may spend: seconds left and model calls allowed."""

    seconds: float
    model_calls: int


async def measure_budget(ctx: RunContext) -> Budget:
    """Read the seconds left, then make model calls until the limit stops them."""
    seconds = ctx.deadline - asyncio.get_running_loop().time()
    calls = 0
    with contextlib.suppress(ModelCallLimitExceededError):
        while True:
            await ctx.generate("Count this call.")
            calls += 1
    return Budget(seconds=seconds, model_calls=calls)


@agent(version="1.0.0")
class Planner(Agent):
    """Reports its budget under the library defaults."""

    @skill("budget", tags=["limits"])
    async def budget(self, ctx: RunContext) -> Budget:
        """Report this run's budget."""
        return await measure_budget(ctx)

    @skill("quick_budget", tags=["limits"], limits=RunLimits(timeout_seconds=2))
    async def quick_budget(self, ctx: RunContext) -> Budget:
        """Report the budget of a skill with its own timeout."""
        return await measure_budget(ctx)


@agent(version="1.0.0")
class CarefulPlanner(Agent):
    """Reports its budget under agent and skill limits."""

    limits = RunLimits(timeout_seconds=60, max_model_calls=3)

    @skill("budget", tags=["limits"])
    async def budget(self, ctx: RunContext) -> Budget:
        """Report the budget the agent's limits give."""
        return await measure_budget(ctx)

    @skill("quick_budget", tags=["limits"], limits=RunLimits(timeout_seconds=30))
    async def quick_budget(self, ctx: RunContext) -> Budget:
        """Report the budget of a skill that sets only its timeout."""
        return await measure_budget(ctx)


def counting_model() -> ScriptedModel:
    return ScriptedModel(["Counted."] * 20)


def assert_budget(budget: Budget, seconds: float, model_calls: int) -> None:
    assert seconds - 1 < budget.seconds <= seconds
    assert budget.model_calls == model_calls


@pytest.mark.parametrize(
    ("planner_class", "skill_id", "options", "seconds", "model_calls"),
    [
        pytest.param(Planner, "budget", None, 120, 10, id="library defaults"),
        pytest.param(CarefulPlanner, "budget", None, 60, 3, id="agent overrides"),
        pytest.param(
            CarefulPlanner,
            "quick_budget",
            None,
            30,
            3,
            id="skill overrides only the field it sets",
        ),
        pytest.param(
            CarefulPlanner,
            "quick_budget",
            RunOptions(limits=RunLimits(max_model_calls=2)),
            30,
            2,
            id="run overrides only the field it sets",
        ),
    ],
)
async def test_limits_resolve_library_then_agent_then_skill_then_run(
    planner_class: type[Agent],
    skill_id: str,
    options: RunOptions | None,
    seconds: float,
    model_calls: int,
) -> None:
    planner = planner_class(model=counting_model())

    budget = await Runtime().invoke(planner, skill=skill_id, options=options)

    assert_budget(budget, seconds, model_calls)


async def test_a_host_ceiling_caps_defaults_but_keeps_tighter_skill_limits(
    serve: ServeLocally,
) -> None:
    def capped_app(public_url: str) -> AgentApp:
        return AgentApp(
            agent=Planner(model=counting_model()),
            host=HostConfig(
                public_url=public_url,
                auth=APIKeyAuth(keys=API_KEYS),
                limits_ceiling=RunLimits(timeout_seconds=5, max_model_calls=4),
            ),
        )

    async with (
        serve(capped_app) as base_url,
        await connect(base_url, ALICE_KEY) as alice,
    ):
        default_task = await call_skill(alice, "budget")
        quick_task = await call_skill(alice, "quick_budget")

    assert_budget(
        Budget.model_validate(value_of(default_task)), seconds=5, model_calls=4
    )
    assert_budget(Budget.model_validate(value_of(quick_task)), seconds=2, model_calls=4)


def test_a_limit_explicitly_above_the_host_ceiling_is_refused_at_startup() -> None:
    with pytest.raises(DefinitionError, match="ceiling"):
        AgentApp(
            agent=CarefulPlanner(model=counting_model()),
            host=HostConfig(
                public_url="https://planner.example.com",
                auth=APIKeyAuth(keys=API_KEYS),
                limits_ceiling=RunLimits(timeout_seconds=5),
            ),
        )


# 2. Deadlines: one deadline for everything a run waits on


class Cabinet:
    """A per-run filing cabinet."""


@agent(version="1.0.0")
class Clerk(Agent):
    """Files forms with a model's help, within one second."""

    limits = RunLimits(timeout_seconds=1)

    @skill("file", tags=["forms"])
    async def file(self, form: str, ctx: RunContext, cabinet: Inject[Cabinet]) -> str:
        """File a form."""
        return await ctx.generate(f"File the form {form}.")


def cabinet_services(setup_seconds: float = 0) -> Services:
    async def open_cabinet() -> Cabinet:
        await asyncio.sleep(setup_seconds)
        return Cabinet()

    services = Services()
    services.add_scoped(Cabinet, open_cabinet)
    return services


async def test_policy_checks_service_setup_and_model_calls_share_one_deadline() -> None:
    async def slow_input_check(
        skill: SkillDefinition, principal: Principal | None, arguments: object
    ) -> bool:
        await asyncio.sleep(0.3)
        return True

    runtime = Runtime(
        services=cabinet_services(setup_seconds=0.3),
        authorization=SlowAuthorization(0.3),
        policy=ExecutionPolicy(input_checks=[slow_input_check]),
    )
    # Each step alone fits the one-second timeout; together they do not.
    clerk = Clerk(model=SlowModel(["Filed."], seconds=0.3))

    started = time.monotonic()
    with pytest.raises(TimeoutLimitExceededError) as raised:
        await runtime.run(clerk, skill="file", arguments={"form": "W-4"})

    assert 0.95 <= elapsed_since(started) < 1.5
    assert raised.value.invocation_id


async def test_a_retry_wait_that_would_pass_the_deadline_is_not_taken() -> None:
    model = ScriptedModel(
        [ProviderError("Rate limited.", transient=True, retry_after=0.6), "Filed."]
    )
    # The policy uses half the second, so the 0.6-second wait no longer fits.
    runtime = Runtime(services=cabinet_services(), authorization=SlowAuthorization(0.5))

    started = time.monotonic()
    with pytest.raises(ProviderError):
        await runtime.run(Clerk(model=model), skill="file", arguments={"form": "W-4"})

    assert elapsed_since(started) < 0.9
    assert len(model.requests) == 1


class SlowRouter(SkillRouter):
    """Takes ``seconds`` to route every message to a short nap."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    async def route(
        self,
        conversation: Sequence[Message],
        skills: Sequence[SkillDefinition],
        ctx: RoutingContext,
    ) -> SkillCall | Clarification:
        await asyncio.sleep(self.seconds)
        return SkillCall("nap", {"seconds": 0.3})


async def test_routing_time_counts_against_the_skills_deadline(
    serve: ServeLocally, bank: Bank
) -> None:
    def routed_app(public_url: str) -> AgentApp:
        return bank.app(public_url, router=SlowRouter(0.3))

    async with (
        serve(routed_app) as base_url,
        await connect(base_url, ALICE_KEY) as alice,
    ):
        started = time.monotonic()
        # Routing (0.3 s) plus the nap (0.3 s) exceed nap's 0.5-second timeout.
        task = await say(alice, "Take a short nap.")

    assert task.status.state == FAILED
    assert status_text(task) == "Skill 'nap' exceeded its timeout of 0.5 seconds"
    assert elapsed_since(started) < 1


# 3. Cancellation


@agent(version="1.0.0")
class Archive(Agent):
    """Reads records over a per-run connection."""

    def __init__(self) -> None:
        super().__init__()
        self.reading = asyncio.Event()

    @skill("read", tags=["records"], limits=RunLimits(timeout_seconds=0.2))
    async def read(
        self, outcome: str, ctx: RunContext, connection: Inject[Connection]
    ) -> str:
        """Succeed, fail or hang, as the caller asks."""
        self.reading.set()
        if outcome == "fail":
            raise RuntimeError("The disk failed.")
        if outcome == "hang":
            await asyncio.sleep(5)
        return "A record."


async def test_cancelling_a_local_run_reraises_asyncio_cancellation_unchanged() -> None:
    connections = Connections()
    archive = Archive()
    runtime = Runtime(services=connections.services())
    running = asyncio.create_task(
        runtime.run(archive, skill="read", arguments={"outcome": "hang"})
    )
    await archive.reading.wait()

    running.cancel()

    # Not AgentFrame's CancelledError: that only reports a cancelled hosted task.
    with pytest.raises(asyncio.CancelledError):
        await running
    assert [c.closed for c in connections.opened] == [True]


async def test_cancelling_a_local_run_during_a_policy_check_reraises_it_too() -> None:
    check_started = asyncio.Event()

    async def hanging_input_check(
        skill: SkillDefinition, principal: Principal | None, arguments: object
    ) -> bool:
        check_started.set()
        await asyncio.Event().wait()
        return True

    runtime = Runtime(
        services=Connections().services(),
        policy=ExecutionPolicy(input_checks=[hanging_input_check]),
    )
    running = asyncio.create_task(
        runtime.run(Archive(), skill="read", arguments={"outcome": "succeed"})
    )
    await check_started.wait()

    running.cancel()

    with pytest.raises(asyncio.CancelledError):
        await running


async def test_cancel_task_ends_a_hosted_run_canceled_and_cleans_up(
    alice: Client, bank: Bank
) -> None:
    task = await start_waiting(alice, bank.desk)

    cancelled = await alice.cancel_task(CancelTaskRequest(id=task.id))

    assert cancelled.status.state == TaskState.TASK_STATE_CANCELED
    assert status_text(cancelled) == "The task was cancelled."
    assert invocation_id(cancelled)
    await eventually(lambda: bank.connections.opened[0].closed)


async def test_shutdown_cancels_runs_still_going_after_the_grace_period(
    bank: Bank,
) -> None:
    app = bank.app(IN_PROCESS_URL, shutdown_grace_seconds=0.3)

    async with in_process(app) as (lifespan, alice):
        task = await start_waiting(alice, bank.desk)

        seconds = await lifespan.shutdown()
        stored = await get_task(alice, task.id)

    assert 0.3 <= seconds < 1.5
    assert stored.status.state == TaskState.TASK_STATE_CANCELED
    assert invocation_id(stored)
    assert [c.closed for c in bank.connections.opened] == [True]


# 4. Services: startup checks and per-run cleanup


def test_a_missing_service_fails_startup_before_any_run() -> None:
    with pytest.raises(DefinitionError, match="no service registered"):
        AgentApp(
            agent=Archive(),
            host=HostConfig(
                public_url="https://archive.example.com",
                auth=APIKeyAuth(keys=API_KEYS),
            ),
        )


@pytest.mark.parametrize(
    ("outcome", "error"),
    [
        pytest.param("succeed", None, id="success"),
        pytest.param("fail", RuntimeError, id="failure"),
        pytest.param("hang", TimeoutLimitExceededError, id="timeout"),
    ],
)
async def test_a_per_run_service_closes_however_the_run_ends(
    outcome: str, error: type[Exception] | None
) -> None:
    connections = Connections()
    runtime = Runtime(services=connections.services())

    with pytest.raises(error) if error else contextlib.nullcontext():
        await runtime.run(Archive(), skill="read", arguments={"outcome": outcome})

    # Cancellation is covered by the cancellation tests above.
    assert [c.closed for c in connections.opened] == [True]


# 5. RunResult without telemetry


@agent(version="1.0.0")
class Editor(Agent):
    """Drafts, then polishes, with two model calls."""

    @skill("edit", tags=["writing"])
    async def edit(self, text: str, ctx: RunContext) -> str:
        """Edit a text."""
        draft = await ctx.generate(f"Draft an edit of: {text}")
        return await ctx.generate(f"Polish: {draft}")


async def test_run_result_reports_execution_metadata_without_tracing() -> None:
    usage = Usage(input_tokens=12, output_tokens=3, total_tokens=15, estimated=False)
    model = ScriptedModel(
        [
            ModelResponse("Draft.", usage, FinishReason.COMPLETED),
            ModelResponse("Polished.", usage, FinishReason.COMPLETED),
        ]
    )

    result = await Runtime().run(
        Editor(model=model), skill="edit", arguments={"text": "Hi"}
    )

    assert result.value == "Polished."
    assert uuid.UUID(result.invocation_id)
    assert result.usage == Usage(24, 6, 30, estimated=False)
    assert result.model_calls == 2
    assert result.tool_calls == 0
    assert result.duration.total_seconds() > 0
    assert result.finish_reason == FinishReason.COMPLETED


# 6. Safe, correlated errors and redacted logs


@pytest.mark.parametrize(
    ("skill_id", "arguments", "state"),
    [
        pytest.param(
            "statement", {"account": SECRET_ACCOUNT}, FAILED, id="skill exception"
        ),
        pytest.param("advise", {"question": SECRET_PROMPT}, FAILED, id="model failure"),
        pytest.param("nap", {"seconds": 5}, FAILED, id="timeout"),
        pytest.param(
            "balance",
            {"account": {"number": SECRET_ACCOUNT}},
            REJECTED,
            id="invalid arguments",
        ),
        pytest.param(
            "transfer",
            {"account": SECRET_ACCOUNT, "amount": 5},
            REJECTED,
            id="missing permission",
        ),
        pytest.param("balance", {"account": "frozen"}, REJECTED, id="policy denial"),
        pytest.param("teleport", {"to": SECRET_ACCOUNT}, REJECTED, id="unknown skill"),
    ],
)
async def test_every_hosted_failure_has_an_invocation_id_and_a_safe_message(
    alice: Client, skill_id: str, arguments: dict[str, object], state: TaskState
) -> None:
    task = await call_skill(alice, skill_id, arguments)

    reply = agent_reply(task)
    assert task.status.state == state
    assert invocation_id(task)
    assert status_text(task)
    assert not task.artifacts
    assert SECRET_ACCOUNT not in reply
    assert SECRET_PROMPT not in reply
    assert "Traceback" not in reply


async def test_server_logs_carry_invocation_ids_but_no_arguments_prompts_or_keys(
    base_url: str, alice: Client, bank: Bank, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.DEBUG, logger="agentframe")

    crashed = await call_skill(alice, "statement", {"account": SECRET_ACCOUNT})
    model_failed = await call_skill(alice, "advise", {"question": SECRET_PROMPT})
    bank.ledger.available = False
    policy_down = await call_skill(alice, "balance", {"account": SECRET_ACCOUNT})
    async with await connect(base_url, SECRET_KEY) as stranger:
        with pytest.raises(A2AClientError):
            await call_skill(stranger, "balance", {"account": SECRET_ACCOUNT})

    for task in (crashed, model_failed, policy_down):
        assert task.status.state == FAILED
        assert invocation_id(task) in caplog.text
    for secret in (SECRET_ACCOUNT, SECRET_PROMPT, SECRET_KEY, ALICE_KEY):
        assert secret not in caplog.text
    # Operators keep the skill's traceback, under the ID the caller can quote.
    [skill_failure] = [r for r in caplog.records if r.exc_info]
    assert invocation_id(crashed) in skill_failure.getMessage()


# 7. Identity, resource and policy failures


@pytest.mark.parametrize("api_key", [None, SECRET_KEY])
async def test_a_missing_or_wrong_api_key_gets_401(
    base_url: str, api_key: str | None
) -> None:
    async with await connect(base_url, api_key) as stranger:
        with pytest.raises(A2AClientError) as raised:
            await call_skill(stranger, "balance", {"account": "A1"})

    response = http_error(raised)
    assert response.status_code == 401
    assert response.json() == {"error": "Authentication required."}


async def test_permissions_come_only_from_the_callers_verified_roles(
    alice: Client, bob: Client
) -> None:
    arguments = {"account": "A1", "amount": 5}

    denied = await call_skill(alice, "transfer", arguments)
    allowed = await call_skill(bob, "transfer", arguments)

    assert denied.status.state == REJECTED
    assert status_text(denied) == (
        "Skill 'transfer' requires permissions: payments:write"
    )
    assert allowed.status.state == TaskState.TASK_STATE_COMPLETED


async def test_a_resource_policy_outage_fails_closed(alice: Client, bank: Bank) -> None:
    bank.ledger.available = False

    task = await call_skill(alice, "balance", {"account": "A1"})

    assert task.status.state == FAILED
    assert (
        status_text(task) == "Authorization policy for skill 'balance' is unavailable"
    )


async def test_an_execution_policy_denial_is_rejected(bob: Client) -> None:
    task = await call_skill(bob, "transfer", {"account": "A1", "amount": 5000})

    assert task.status.state == REJECTED
    assert status_text(task) == (
        "Skill 'transfer' was blocked by an execution policy check"
    )


OTHER_GUID = "00000000-0000-0000-0000-000000000000"


def expired(entra: FakeEntra) -> str:
    return entra.token(exp=int(time.time()) - 3600)


def for_another_api(entra: FakeEntra) -> str:
    return entra.token(aud=OTHER_GUID)


def from_another_tenant(entra: FakeEntra) -> str:
    return entra.token(tid=OTHER_GUID)


def version_1(entra: FakeEntra) -> str:
    return entra.token(ver="1.0")


def signed_by_a_stranger(entra: FakeEntra) -> str:
    return sign(entra.claims(), new_rsa_key(), "key-unknown")


@pytest.mark.parametrize(
    "make_token",
    [
        expired,
        for_another_api,
        from_another_tenant,
        version_1,
        signed_by_a_stranger,
    ],
)
async def test_a_valid_entra_token_runs_and_invalid_ones_get_401(
    serve: ServeLocally, bank: Bank, make_token: Callable[[FakeEntra], str]
) -> None:
    entra = FakeEntra()
    auth = EntraIDAuth(
        tenant_id=TENANT_ID, audience=API_CLIENT_ID, transport=entra.transport
    )
    invalid_token = make_token(entra)

    async with serve(lambda url: bank.app(url, auth=auth)) as base_url:
        async with await connect(base_url, entra.token()) as editor:
            accepted = await call_skill(
                editor, "transfer", {"account": "A", "amount": 1}
            )
        async with await connect(base_url, invalid_token) as stranger:
            with pytest.raises(A2AClientError) as raised:
                await call_skill(stranger, "balance", {"account": "A1"})

    assert accepted.status.state == TaskState.TASK_STATE_COMPLETED
    assert http_error(raised).status_code == 401
    assert invalid_token not in http_error(raised).text


async def test_an_entra_outage_fails_closed_with_401(
    serve: ServeLocally, bank: Bank
) -> None:
    entra = FakeEntra()
    entra.available = False
    auth = EntraIDAuth(
        tenant_id=TENANT_ID, audience=API_CLIENT_ID, transport=entra.transport
    )

    async with (
        serve(lambda url: bank.app(url, auth=auth)) as base_url,
        await connect(base_url, entra.token()) as editor,
    ):
        with pytest.raises(A2AClientError) as raised:
            await call_skill(editor, "balance", {"account": "A1"})

    assert http_error(raised).status_code == 401


# 8. Host protection


async def test_a_full_host_answers_503_with_retry_after_and_creates_no_task(
    serve: ServeLocally, bank: Bank
) -> None:
    def one_run_app(public_url: str) -> AgentApp:
        return bank.app(public_url, max_concurrent_runs=1)

    async with (
        serve(one_run_app) as base_url,
        await connect(base_url, ALICE_KEY) as alice,
    ):
        waiting = await start_waiting(alice, bank.desk)
        with pytest.raises(A2AClientError) as raised:
            await call_skill(alice, "echo", {"text": "Hi"})
        bank.desk.release.set()
        await wait_until_finished(alice, waiting.id)
        retried = await call_skill(alice, "echo", {"text": "Hi"})
        tasks = await alice.list_tasks(ListTasksRequest())

    response = http_error(raised)
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert bank.desk.echoes == 1
    assert retried.status.state == TaskState.TASK_STATE_COMPLETED
    assert sorted(t.id for t in tasks.tasks) == sorted([waiting.id, retried.id])


async def test_request_and_response_byte_limits(
    serve: ServeLocally, bank: Bank
) -> None:
    def small_app(public_url: str) -> AgentApp:
        return bank.app(public_url, max_request_bytes=4096, max_response_bytes=1024)

    async with (
        serve(small_app) as base_url,
        await connect(base_url, ALICE_KEY) as alice,
    ):
        with pytest.raises(A2AClientError) as too_large:
            await call_skill(alice, "echo", {"text": "x" * 5000})
        too_large_result = await call_skill(alice, "echo", {"text": "x" * 2000})

    assert http_error(too_large).status_code == 413
    assert too_large_result.status.state == FAILED
    assert status_text(too_large_result) == (
        "The result exceeds the host's response size limit"
    )
    assert "xxxx" not in agent_reply(too_large_result)


async def test_probes_answer_without_credentials(base_url: str) -> None:
    async with httpx.AsyncClient(base_url=base_url) as http:
        health = await http.get("/health")
        ready = await http.get("/ready")

    assert (health.status_code, health.json()) == (200, {"status": "ok"})
    assert (ready.status_code, ready.json()) == (200, {"status": "ready"})


async def test_draining_refuses_new_messages_but_lets_running_tasks_finish(
    bank: Bank,
) -> None:
    app = bank.app(IN_PROCESS_URL)

    async with in_process(app) as (lifespan, alice):
        task = await start_waiting(alice, bank.desk)
        app.begin_shutdown()
        with pytest.raises(A2AClientError) as refused:
            await call_skill(alice, "echo", {"text": "Hi"})
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=IN_PROCESS_URL
        ) as http:
            readiness = await http.get("/ready")
        bank.desk.release.set()
        finished = await wait_until_finished(alice, task.id)
        await lifespan.shutdown()

    assert http_error(refused).status_code == 503
    assert http_error(refused).headers["Retry-After"] == "1"
    assert readiness.status_code == 503
    assert finished.status.state == TaskState.TASK_STATE_COMPLETED
    assert get_artifact_text(finished.artifacts[0]) == "Done."


def echo_message(text: str, message_id: str) -> Message:
    return Message(
        message_id=message_id,
        role=Role.ROLE_USER,
        parts=[new_data_part({"text": text})],
        metadata={"agentframe.skill": "echo"},
    )


async def test_a_repeated_message_returns_the_original_task_per_caller(
    alice: Client, bob: Client, bank: Bank
) -> None:
    message_id = str(uuid.uuid4())

    first = await send(alice, echo_message("Hi", message_id), skill_call=True)
    repeat = await send(alice, echo_message("Hi", message_id), skill_call=True)
    bobs = await send(bob, echo_message("Hi", message_id), skill_call=True)
    with pytest.raises(InvalidParamsError, match="used for a different message"):
        await send(alice, echo_message("Bye", message_id), skill_call=True)

    assert repeat.id == first.id
    assert bobs.id != first.id
    assert bank.desk.echoes == 2


async def test_callers_cannot_see_or_cancel_each_others_tasks(
    alice: Client, bob: Client, bank: Bank
) -> None:
    task = await start_waiting(alice, bank.desk)

    with pytest.raises(TaskNotFoundError):
        await get_task(bob, task.id)
    with pytest.raises(TaskNotFoundError):
        await bob.cancel_task(CancelTaskRequest(id=task.id))
    bank.desk.release.set()
    finished = await wait_until_finished(alice, task.id)

    assert finished.status.state == TaskState.TASK_STATE_COMPLETED
