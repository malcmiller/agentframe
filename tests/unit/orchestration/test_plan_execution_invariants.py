"""Plan execution's internal invariants are explicit checks, not ``assert``.

``python -O`` removes ``assert`` statements, so these checks would vanish
there. ``test_optimized_mode.py`` runs this file under ``python -O`` too.
Plan execution is tested end to end in ``test_run_plan_hosted.py``.
"""

import asyncio
from collections.abc import Mapping
from contextlib import AsyncExitStack
from typing import Any

import pytest

from agentframe import ExecutionPolicy, Principal, Services
from agentframe.authoring.definitions import SkillDefinition
from agentframe.models.base import DEFAULT_MODEL_SETTINGS
from agentframe.orchestration.catalog import RemoteSkill, SkillCatalog
from agentframe.orchestration.execution import (
    _check_plan,
    _equal_share,
    _Execution,
)
from agentframe.orchestration.plan import Plan, PlanStep
from agentframe.runtime.correlation import RunCorrelation
from agentframe.runtime.generation import GenerationState
from agentframe.runtime.limits import DEFAULT_LIMITS, RunLimits

PLAN = Plan(steps=[PlanStep(id="a", use="research-agent.find_sources")], result="a")
FIND_SOURCES = RemoteSkill(
    name="research-agent.find_sources",
    agent="research-agent",
    skill="find_sources",
    description="Find sources.",
    tags=(),
)


async def allow_plan(
    skill: SkillDefinition, principal: Principal | None, plan: Plan
) -> bool:
    return True


async def allow_tool(
    skill: SkillDefinition,
    principal: Principal | None,
    tool: str,
    arguments: Mapping[str, Any],
) -> bool:
    return True


def execution(
    state: GenerationState, catalog: SkillCatalog | None = None
) -> _Execution:
    return _Execution(
        catalog or SkillCatalog([], bindings={}),
        state,
        RunCorrelation("planner", "run-1", root_invocation_id="run-1"),
        None,
        deadline(),
        max_parallel=1,
        tokens_per_step=100,
        on_failure="stop",
    )


def generation(
    limits: RunLimits = DEFAULT_LIMITS, policy: ExecutionPolicy | None = None
) -> GenerationState:
    # No skill: the state routing would have, which never runs a plan.
    return GenerationState(
        model=None,
        instructions=None,
        settings=DEFAULT_MODEL_SETTINGS,
        limits=limits,
        services=Services().scope(AsyncExitStack()),
        policy=policy,
    )


def deadline() -> float:
    return asyncio.get_running_loop().time() + 5


async def test_invariant_step_outside_the_catalog_is_refused_before_counting() -> None:
    state = generation()

    with pytest.raises(RuntimeError, match="no entry of this catalog"):
        # noinspection PyProtectedMember
        await execution(state)._send(PLAN.steps[0], started=0.0)

    assert state.accounting.tool_calls == 0


async def test_invariant_plan_checks_need_a_running_skill() -> None:
    state = generation(policy=ExecutionPolicy(plan_checks=[allow_plan]))

    with pytest.raises(RuntimeError, match="outside a skill"):
        await _check_plan(PLAN, state, None, deadline())


async def test_invariant_tool_checks_need_a_running_skill() -> None:
    state = generation(policy=ExecutionPolicy(tool_checks=[allow_tool]))

    with pytest.raises(RuntimeError, match="outside a skill"):
        # noinspection PyProtectedMember
        await execution(state)._check(FIND_SOURCES, {"topic": "tides"})


def test_invariant_token_share_needs_a_token_limit() -> None:
    state = generation(limits=RunLimits())

    with pytest.raises(RuntimeError, match="max_total_tokens"):
        _equal_share(state, step_count=2)
