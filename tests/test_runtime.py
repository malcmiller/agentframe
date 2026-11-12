import asyncio
import socket
import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest

from agentframe import (
    Agent,
    FinishReason,
    InputValidationError,
    LimitExceededError,
    PermissionDeniedError,
    Principal,
    RunContext,
    RunLimits,
    Runtime,
    Usage,
    agent,
    skill,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("tests must not open sockets")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    yield


@agent(version="1.0.0")
class Vault(Agent):
    """Stores and reveals secrets."""

    limits = RunLimits(timeout_seconds=5)

    @skill(id="echo", tags=["test"])
    async def echo(self, text: str, ctx: RunContext) -> str:
        """Return the text."""
        return text

    @skill(id="context", tags=["test"])
    async def context(self, ctx: RunContext) -> RunContext:
        """Return the run context."""
        return ctx

    @skill(
        id="reveal", requires_permissions=["vault.read", "vault.audit"], tags=["test"]
    )
    async def reveal(self, ctx: RunContext) -> str:
        """Reveal the secret."""
        return "secret"

    @skill(id="slow", limits=RunLimits(timeout_seconds=0.01), tags=["test"])
    async def slow(self, ctx: RunContext) -> None:
        """Take too long."""
        await asyncio.sleep(10)

    @skill(id="own_timeout", tags=["test"])
    async def own_timeout(self, ctx: RunContext) -> None:
        """Raise a TimeoutError that is not the run timeout."""
        raise TimeoutError


async def test_invoke_returns_the_value() -> None:
    assert (
        await Runtime().invoke(Vault(), skill="echo", arguments={"text": "hi"}) == "hi"
    )


async def test_run_returns_a_result_with_metadata() -> None:
    result = await Runtime().run(Vault(), skill="echo", arguments={"text": "hi"})

    assert result.value == "hi"
    assert uuid.UUID(result.invocation_id)
    assert result.usage == Usage(0, 0, 0, estimated=False)
    assert (result.model_calls, result.tool_calls) == (0, 0)
    assert result.finish_reason is FinishReason.COMPLETED
    assert result.duration > timedelta(0)


async def test_each_run_gets_its_own_id_and_context() -> None:
    runtime = Runtime()
    principal = Principal(id="ada")

    first = await runtime.run(Vault(), skill="context", principal=principal)
    second = await runtime.run(Vault(), skill="context")

    assert first.invocation_id != second.invocation_id
    assert first.value is not second.value
    assert first.value.invocation_id == first.invocation_id
    assert first.value.principal == principal
    assert second.value.principal is None


async def test_context_deadline_follows_the_agent_timeout() -> None:
    before = asyncio.get_running_loop().time()

    ctx = await Runtime().invoke(Vault(), skill="context")

    assert before + 5 <= ctx.deadline <= asyncio.get_running_loop().time() + 5


async def test_unknown_skill_is_rejected() -> None:
    with pytest.raises(InputValidationError, match="no skill 'missing'"):
        await Runtime().invoke(Vault(), skill="missing")


async def test_skill_without_permissions_needs_no_principal() -> None:
    assert await Runtime().invoke(Vault(), skill="echo", arguments={"text": "ok"})


async def test_protected_skill_without_principal_is_denied() -> None:
    with pytest.raises(PermissionDeniedError, match="caller identity"):
        await Runtime().invoke(Vault(), skill="reveal")


async def test_principal_missing_one_permission_is_denied() -> None:
    reader = Principal(id="ada", permissions=frozenset({"vault.read"}))

    with pytest.raises(PermissionDeniedError, match=r"vault\.audit"):
        await Runtime().invoke(Vault(), skill="reveal", principal=reader)


async def test_principal_with_all_permissions_is_allowed() -> None:
    auditor = Principal(id="ada", permissions=frozenset({"vault.read", "vault.audit"}))

    assert (
        await Runtime().invoke(Vault(), skill="reveal", principal=auditor) == "secret"
    )


async def test_slow_skill_exceeds_its_timeout() -> None:
    with pytest.raises(LimitExceededError, match=r"0\.01 seconds"):
        await Runtime().invoke(Vault(), skill="slow")


async def test_timeout_error_from_the_skill_is_not_a_limit() -> None:
    with pytest.raises(TimeoutError) as caught:
        await Runtime().invoke(Vault(), skill="own_timeout")

    assert not isinstance(caught.value, LimitExceededError)


@pytest.mark.parametrize(
    ("skill_id", "error_type"),
    [
        ("missing", InputValidationError),
        ("reveal", PermissionDeniedError),
        ("slow", LimitExceededError),
    ],
)
async def test_errors_carry_the_invocation_id(
    skill_id: str, error_type: type[Exception]
) -> None:
    with pytest.raises(error_type) as caught:
        await Runtime().invoke(Vault(), skill=skill_id)

    invocation_id = getattr(caught.value, "invocation_id", None)
    assert invocation_id is not None
    assert uuid.UUID(invocation_id)
