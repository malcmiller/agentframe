"""``A2ATarget`` evaluates agents that are actually running, over real A2A.

The AgentFrame peer is an ``AgentApp`` served over localhost HTTP, with a
scripted model that reports usage, an API key and a router for free text.
The plain peer is a bare official-SDK server that advertises nothing.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from a2a.helpers import get_message_text
from a2a.types import Message
from pydantic import BaseModel

from a2a_peer import PeerExecutor, a2a_peer
from agentframe import (
    Agent,
    AgentApp,
    AgentDirectory,
    AgentQuery,
    AgentReference,
    AgentRegistry,
    APIKey,
    APIKeyAuth,
    APIKeyCredential,
    Clarification,
    HostConfig,
    ModelResponse,
    RoutingContext,
    RunContext,
    RunLimits,
    RunOptions,
    Service,
    SkillCall,
    SkillDefinition,
    SkillRouter,
    Usage,
    agent,
    skill,
)
from agentframe.evaluation import (
    A2ATarget,
    Case,
    CaseStatus,
    EvalReport,
    Evaluation,
    contains,
    equals,
)
from agentframe.runtime.usage import FinishReason
from agentframe.testing import ScriptedModel
from local_server import ServeLocally

API_KEY = "evaluation-test-key"


class Summary(BaseModel):
    words: int
    first: str


@agent(name="Writer", description="Summarizes and answers.", version="0.1.0")
class Writer(Agent):
    def __init__(self) -> None:
        super().__init__(model=ScriptedModel(_answer(f"answer {n}") for n in range(9)))
        self.slow_cancelled = asyncio.Event()

    @skill(id="summarize", description="Summarize text.", tags=["writing"])
    async def summarize(self, text: str, ctx: RunContext) -> Summary:
        words = text.split()
        return Summary(words=len(words), first=words[0])

    @skill(id="answer", description="Answer a question.", tags=["writing"])
    async def answer(self, question: str, ctx: RunContext) -> str:
        if question == "slow":
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                self.slow_cancelled.set()
                raise
        if question == "fail":
            raise RuntimeError("secret remote detail")
        return await ctx.generate(question)


def _answer(text: str) -> ModelResponse:
    usage = Usage(input_tokens=30, output_tokens=12, total_tokens=42, estimated=False)
    return ModelResponse(text=text, usage=usage, finish_reason=FinishReason.COMPLETED)


class AnswerRouter(SkillRouter):
    """Routes any text to ``answer``, but asks back for "unclear"."""

    async def route(
        self,
        conversation: Sequence[Message],
        skills: Sequence[SkillDefinition],
        ctx: RoutingContext,
    ) -> SkillCall | Clarification:
        text = get_message_text(conversation[-1])
        if text == "unclear":
            return Clarification("Which topic do you mean?")
        return SkillCall("answer", {"question": text})


class OneAgent(AgentRegistry):
    """Lists one agent, ``writer``, at a card URL."""

    def __init__(self, base_url: str) -> None:
        self.card_url = f"{base_url}/.well-known/agent-card.json"

    async def get(self, agent_id: str) -> AgentReference:
        return AgentReference(agent_id, self.card_url)

    async def list(self, query: AgentQuery) -> list[AgentReference]:
        return [AgentReference("writer", self.card_url)]


@pytest.fixture
def writer() -> Writer:
    return Writer()


@pytest.fixture
async def directory(
    serve: ServeLocally, writer: Writer
) -> AsyncIterator[AgentDirectory]:
    def app(public_url: str) -> AgentApp:
        auth = APIKeyAuth(keys={API_KEY: APIKey("evaluation")})
        return AgentApp(
            agent=writer,
            host=HostConfig(public_url=public_url, auth=auth),
            router=AnswerRouter(),
        )

    async with serve(app) as url, AgentDirectory(sources=[OneAgent(url)]) as found:
        yield found


IDENTITY = Service(credential=APIKeyCredential(API_KEY))


async def evaluate(target: A2ATarget, *cases: Case[Any]) -> EvalReport:
    evaluation = Evaluation(cases=cases, target=target, scorers=[equals])
    return await evaluation.run()


async def test_structured_cases_call_the_skill_and_validate_the_output(
    directory: AgentDirectory,
) -> None:
    target = A2ATarget(
        directory, "writer", skill="summarize", output=Summary, identity=IDENTITY
    )

    report = await evaluate(
        target,
        Case(
            id="three",
            arguments={"text": "one two three"},
            expected={"words": 3, "first": "one"},
        ),
        Case(
            id="wrong",
            arguments={"text": "a b"},
            expected={"words": 3, "first": "a"},
        ),
    )

    passed, failed = report.results
    assert passed.status is CaseStatus.PASSED
    assert failed.status is CaseStatus.FAILED
    assert passed.invocation_id and passed.duration is not None
    assert passed.model_calls is None and passed.finish_reason is None


async def test_free_text_cases_ask_and_report_the_peers_usage(
    directory: AgentDirectory,
) -> None:
    target = A2ATarget(directory, "writer", identity=IDENTITY)
    evaluation = Evaluation(
        cases=[Case(id="tides", text="What are tides?")],
        target=target,
        scorers=[contains("answer")],
    )

    [result] = (await evaluation.run()).results

    assert result.status is CaseStatus.PASSED
    assert result.usage == Usage(
        input_tokens=30, output_tokens=12, total_tokens=42, estimated=False
    )


async def test_a_peer_without_the_extension_reports_unknown_usage(
    serve: ServeLocally,
) -> None:
    async with (
        serve(a2a_peer(PeerExecutor())) as url,
        AgentDirectory(sources=[OneAgent(url)]) as directory,
    ):
        target = A2ATarget(directory, "writer")
        report = await evaluate(target, Case(id="hi", text="hi", expected="Echo: hi"))

    [result] = report.results
    assert result.status is CaseStatus.PASSED
    assert result.usage is not None and result.usage.total_tokens is None
    assert report.total_usage.unknown_cases == 1


async def test_a_slow_peer_is_cut_off_at_the_case_deadline_and_cancelled(
    directory: AgentDirectory, writer: Writer
) -> None:
    options = RunOptions(limits=RunLimits(timeout_seconds=1))
    target = A2ATarget(directory, "writer", identity=IDENTITY, options=options)

    [result] = (await evaluate(target, Case(id="slow", text="slow"))).results

    assert result.status is CaseStatus.ERRORED
    assert result.error_type == "TimeoutLimitExceededError"
    assert result.invocation_id
    await asyncio.wait_for(writer.slow_cancelled.wait(), timeout=5)


@pytest.mark.parametrize(
    ("text", "error_type"),
    [
        ("fail", "RemoteAgentError"),
        ("unclear", "ClarificationNeededError"),
    ],
)
async def test_remote_failures_mark_the_case_errored_with_a_safe_message(
    directory: AgentDirectory, text: str, error_type: str
) -> None:
    target = A2ATarget(directory, "writer", identity=IDENTITY)

    [result] = (await evaluate(target, Case(id="bad", text=text))).results

    assert result.status is CaseStatus.ERRORED
    assert result.error_type == error_type
    assert result.error_message and "secret" not in result.error_message
    assert "Which topic" not in result.error_message
    assert result.invocation_id


async def test_a_card_that_requires_a_key_errors_without_an_identity(
    directory: AgentDirectory,
) -> None:
    target = A2ATarget(directory, "writer")

    [result] = (await evaluate(target, Case(id="tides", text="tides"))).results

    assert result.status is CaseStatus.ERRORED
    assert result.error_type == "DefinitionError"


async def test_the_case_token_ceiling_is_sent_to_the_peer(
    directory: AgentDirectory,
) -> None:
    # Too small for the peer's model call, so the peer refuses to make it.
    options = RunOptions(limits=RunLimits(max_total_tokens=5))
    target = A2ATarget(directory, "writer", identity=IDENTITY, options=options)

    [result] = (await evaluate(target, Case(id="tides", text="tides"))).results

    assert result.status is CaseStatus.ERRORED
    assert result.error_type == "RemoteAgentError"


async def test_concurrent_repeats_fit_the_budget_with_the_peers_usage(
    directory: AgentDirectory,
) -> None:
    # Each attempt holds 5000 and settles at the 42 the peer reports, so four
    # attempts fit a budget that holds only two at once.
    options = RunOptions(limits=RunLimits(max_total_tokens=5000))
    target = A2ATarget(directory, "writer", identity=IDENTITY, options=options)
    evaluation = Evaluation(
        cases=[Case(id="a", text="a"), Case(id="b", text="b")],
        target=target,
        scorers=[contains("answer")],
        concurrency=2,
        repeats=2,
        max_total_tokens=10_000,
    )

    report = await evaluation.run()

    assert [(r.case_id, r.attempt) for r in report.results] == [
        ("a", 1),
        ("a", 2),
        ("b", 1),
        ("b", 2),
    ]
    assert report.passed_count == 4
    assert report.total_usage.total_tokens == 4 * 42


async def test_unknown_usage_keeps_the_whole_hold_so_later_cases_do_not_run(
    serve: ServeLocally,
) -> None:
    options = RunOptions(limits=RunLimits(max_total_tokens=1000))
    async with (
        serve(a2a_peer(PeerExecutor())) as url,
        AgentDirectory(sources=[OneAgent(url)]) as directory,
    ):
        evaluation = Evaluation(
            cases=[Case(id=name, text=name) for name in ("a", "b", "c")],
            target=A2ATarget(directory, "writer", options=options),
            scorers=[contains("Echo")],
            concurrency=1,
            max_total_tokens=2000,
        )
        report = await evaluation.run()

    statuses = [result.status for result in report.results]
    assert statuses == [CaseStatus.PASSED, CaseStatus.PASSED, CaseStatus.NOT_RUN]
