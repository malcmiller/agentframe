import asyncio
from collections.abc import Mapping

import pytest

from agentframe import (
    Agent,
    InputValidationError,
    LimitExceededError,
    PermissionDeniedError,
    PolicyUnavailableError,
    Principal,
    RunContext,
    RunLimits,
    Runtime,
    SkillDefinition,
    agent,
    skill,
)

DOCUMENT_OWNERS = {"doc-1": "alice", "doc-2": "bob"}

ALICE = Principal(id="alice", permissions=frozenset({"documents:read"}))


@agent(version="1.0.0")
class Documents(Agent):
    """Reads documents."""

    limits = RunLimits(timeout_seconds=5)

    def __init__(self) -> None:
        self.reads: list[str] = []

    @skill(id="read", requires_permissions=["documents:read"])
    async def read(self, document_id: str, ctx: RunContext) -> str:
        """Return a document's text."""
        self.reads.append(document_id)
        return f"text of {document_id}"


@agent(version="1.0.0")
class SlowReader(Agent):
    """Takes most of its 0.2 second timeout to read."""

    limits = RunLimits(timeout_seconds=0.2)

    def __init__(self) -> None:
        self.finished = False

    @skill(id="read")
    async def read(self, ctx: RunContext) -> str:
        """Read slowly."""
        await asyncio.sleep(0.15)
        self.finished = True
        return "done"


class OwnerOnly:
    """Allows reading only documents the caller owns."""

    def __init__(self) -> None:
        self.seen_arguments: list[Mapping[str, object]] = []

    async def check(
        self,
        principal: Principal | None,
        skill: SkillDefinition,
        arguments: Mapping[str, object],
    ) -> bool:
        self.seen_arguments.append(arguments)
        owner = DOCUMENT_OWNERS.get(str(arguments["document_id"]))
        return principal is not None and owner == principal.id


class BrokenPolicy:
    """Stands in for a policy service that is down."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def check(
        self,
        principal: Principal | None,
        skill: SkillDefinition,
        arguments: Mapping[str, object],
    ) -> bool:
        raise self.error


class SlowPolicy:
    """Waits a fixed time before allowing."""

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


class TruthyPolicy:
    """Returns a truthy value that is not ``True``."""

    async def check(
        self,
        principal: Principal | None,
        skill: SkillDefinition,
        arguments: Mapping[str, object],
    ) -> bool:
        return "yes"  # type: ignore[return-value]


async def test_owner_can_read_their_document() -> None:
    runtime = Runtime(authorization=OwnerOnly())

    text = await runtime.invoke(
        Documents(), skill="read", arguments={"document_id": "doc-1"}, principal=ALICE
    )

    assert text == "text of doc-1"


async def test_permission_does_not_grant_another_callers_document() -> None:
    documents = Documents()
    runtime = Runtime(authorization=OwnerOnly())

    with pytest.raises(PermissionDeniedError) as denied:
        await runtime.invoke(
            documents, skill="read", arguments={"document_id": "doc-2"}, principal=ALICE
        )

    assert documents.reads == []
    assert denied.value.invocation_id is not None


async def test_denial_does_not_reveal_whether_the_document_exists() -> None:
    runtime = Runtime(authorization=OwnerOnly())

    async def denial_message(document_id: str) -> str:
        with pytest.raises(PermissionDeniedError) as denied:
            await runtime.invoke(
                Documents(),
                skill="read",
                arguments={"document_id": document_id},
                principal=ALICE,
            )
        return denied.value.message

    assert await denial_message("doc-2") == await denial_message("missing")
    assert "doc-2" not in await denial_message("doc-2")


async def test_policy_sees_validated_read_only_arguments() -> None:
    policy = OwnerOnly()

    await Runtime(authorization=policy).invoke(
        Documents(), skill="read", arguments={"document_id": "doc-1"}, principal=ALICE
    )

    [arguments] = policy.seen_arguments
    assert arguments == {"document_id": "doc-1"}
    with pytest.raises(TypeError):
        arguments["document_id"] = "doc-2"  # type: ignore[index]


async def test_policy_runs_after_argument_validation() -> None:
    policy = OwnerOnly()

    with pytest.raises(InputValidationError):
        await Runtime(authorization=policy).invoke(
            Documents(), skill="read", arguments={}, principal=ALICE
        )

    assert policy.seen_arguments == []


async def test_policy_runs_after_declared_permissions() -> None:
    policy = OwnerOnly()
    reader_without_grant = Principal(id="alice")

    with pytest.raises(PermissionDeniedError, match=r"documents:read"):
        await Runtime(authorization=policy).invoke(
            Documents(),
            skill="read",
            arguments={"document_id": "doc-1"},
            principal=reader_without_grant,
        )

    assert policy.seen_arguments == []


@pytest.mark.parametrize(
    "error", [RuntimeError("policy service down"), TimeoutError("policy timed out")]
)
async def test_policy_error_fails_closed(error: Exception) -> None:
    documents = Documents()
    runtime = Runtime(authorization=BrokenPolicy(error))

    with pytest.raises(PolicyUnavailableError) as unavailable:
        await runtime.invoke(
            documents, skill="read", arguments={"document_id": "doc-1"}, principal=ALICE
        )

    assert documents.reads == []
    assert "policy service down" not in unavailable.value.message
    assert unavailable.value.invocation_id is not None


async def test_policy_that_outlives_the_run_deadline_fails_closed() -> None:
    reader = SlowReader()
    runtime = Runtime(authorization=SlowPolicy(seconds=10))

    with pytest.raises(PolicyUnavailableError):
        await runtime.invoke(reader, skill="read")

    assert reader.finished is False


async def test_policy_time_counts_against_the_run_timeout() -> None:
    reader = SlowReader()
    runtime = Runtime(authorization=SlowPolicy(seconds=0.1))

    with pytest.raises(LimitExceededError):
        await runtime.invoke(reader, skill="read")

    assert reader.finished is False


async def test_only_true_allows_the_run() -> None:
    with pytest.raises(PermissionDeniedError):
        await Runtime(authorization=TruthyPolicy()).invoke(
            Documents(),
            skill="read",
            arguments={"document_id": "doc-1"},
            principal=ALICE,
        )
