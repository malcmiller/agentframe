"""Tests for field-by-field settings precedence and per-run options.

Precedence, lowest to highest: library defaults -> agent -> skill -> per-run.
A level overrides only the fields it sets; unset fields fall through.
"""

import asyncio
from dataclasses import replace

import pytest

from agentframe import (
    Agent,
    ModelSettings,
    RunContext,
    RunLimits,
    RunOptions,
    Runtime,
    agent,
    skill,
)
from agentframe.generation import GenerationState
from agentframe.limits import DEFAULT_LIMITS
from agentframe.options import merge_settings
from agentframe.testing import ScriptedModel


def defaults_with(*, timeout_seconds: float) -> RunLimits:
    """Return the library default limits with another timeout."""
    return replace(DEFAULT_LIMITS, timeout_seconds=timeout_seconds)


# merge_settings


def test_a_set_field_overrides_the_lower_level() -> None:
    merged = merge_settings(DEFAULT_LIMITS, RunLimits(timeout_seconds=5))

    assert merged == defaults_with(timeout_seconds=5)


def test_an_unset_field_keeps_the_lower_level_value() -> None:
    merged = merge_settings(ModelSettings(max_output_tokens=256), ModelSettings())

    assert merged == ModelSettings(max_output_tokens=256)


def test_missing_levels_are_skipped() -> None:
    merged = merge_settings(DEFAULT_LIMITS, None, RunLimits(timeout_seconds=5), None)

    assert merged == defaults_with(timeout_seconds=5)


def test_the_highest_level_that_sets_a_field_wins() -> None:
    merged = merge_settings(
        DEFAULT_LIMITS,
        RunLimits(timeout_seconds=30),
        RunLimits(timeout_seconds=10),
        RunLimits(),
    )

    assert merged == defaults_with(timeout_seconds=10)


# Through the runtime

AGENT_LIMITS = RunLimits(
    timeout_seconds=30,
    max_total_tokens=50_000,
    max_model_calls=4,
    max_input_tokens=8_000,
    max_model_retries=1,
)


@agent(version="1.0.0")
class Editor(Agent):
    """Edits text."""

    limits = AGENT_LIMITS
    model_settings = ModelSettings(max_output_tokens=512)

    @skill("edit", tags=["text"])
    async def edit(self, text: str, ctx: RunContext) -> object:
        """Edit text with the agent's settings."""
        await ctx.generate(text)
        return ctx

    @skill("brief", model_settings=ModelSettings(max_output_tokens=64), tags=["text"])
    async def brief(self, text: str, ctx: RunContext) -> object:
        """Edit text briefly; only the output size is overridden."""
        await ctx.generate(text)
        return ctx

    @skill("quick", limits=RunLimits(timeout_seconds=5), tags=["text"])
    async def quick(self, text: str, ctx: RunContext) -> object:
        """Edit text quickly; only the timeout is overridden."""
        await ctx.generate(text)
        return ctx

    @skill("unchanged", limits=RunLimits(), model_settings=ModelSettings(), tags=["x"])
    async def unchanged(self, text: str, ctx: RunContext) -> object:
        """Edit text with empty overrides, which change nothing."""
        await ctx.generate(text)
        return ctx


@agent(version="1.0.0")
class Plain(Agent):
    """Sets nothing, so it uses the library defaults."""

    @skill("edit", tags=["text"])
    async def edit(self, text: str, ctx: RunContext) -> object:
        """Edit text with the library defaults."""
        await ctx.generate(text)
        return ctx


def generation_state(ctx: object) -> GenerationState:
    assert isinstance(ctx, RunContext)
    # noinspection PyProtectedMember
    return ctx._generation


async def run_skill(
    agent: Agent, skill_id: str, options: RunOptions | None = None
) -> tuple[RunLimits, ModelSettings, float]:
    """Run a skill; return its effective limits, request settings and timeout."""
    model = ScriptedModel(["Done."])
    agent.model = model
    started = asyncio.get_running_loop().time()

    ctx = await Runtime().invoke(
        agent, skill=skill_id, arguments={"text": "Hi"}, options=options
    )

    limits = generation_state(ctx).limits
    assert generation_state(ctx).settings == model.requests[0].settings
    return limits, model.requests[0].settings, ctx.deadline - started


async def test_an_agent_that_sets_nothing_gets_the_library_defaults() -> None:
    limits, settings, timeout = await run_skill(Plain(), "edit")

    assert limits == DEFAULT_LIMITS
    assert limits == RunLimits(
        timeout_seconds=120,
        max_total_tokens=100_000,
        max_model_calls=10,
        max_model_retries=2,
    )
    assert settings == ModelSettings(max_output_tokens=4096)
    assert timeout == pytest.approx(120, abs=1)


async def test_agent_settings_override_the_library_defaults() -> None:
    limits, settings, timeout = await run_skill(Editor(), "edit")

    assert limits == AGENT_LIMITS
    assert settings == ModelSettings(max_output_tokens=512)
    assert timeout == pytest.approx(30, abs=1)


async def test_a_skill_that_sets_only_model_settings_keeps_the_agent_limits() -> None:
    limits, settings, _ = await run_skill(Editor(), "brief")

    assert limits == AGENT_LIMITS
    assert settings == ModelSettings(max_output_tokens=64)


async def test_a_skill_that_sets_only_timeout_keeps_the_agent_model_settings() -> None:
    limits, settings, timeout = await run_skill(Editor(), "quick")

    assert limits == replace(AGENT_LIMITS, timeout_seconds=5)
    assert settings == ModelSettings(max_output_tokens=512)
    assert timeout == pytest.approx(5, abs=1)


async def test_a_skill_that_sets_only_timeout_keeps_the_agent_token_limits() -> None:
    limits, _, _ = await run_skill(Editor(), "quick")

    assert limits.timeout_seconds == 5
    assert limits.max_total_tokens == 50_000
    assert limits.max_model_calls == 4
    assert limits.max_input_tokens == 8_000


async def test_empty_skill_overrides_keep_every_agent_value() -> None:
    limits, settings, _ = await run_skill(Editor(), "unchanged")

    assert limits == AGENT_LIMITS
    assert settings == ModelSettings(max_output_tokens=512)


async def test_run_options_override_the_skill_and_the_agent() -> None:
    options = RunOptions(
        limits=RunLimits(timeout_seconds=2),
        model_settings=ModelSettings(max_output_tokens=16),
    )

    limits, settings, timeout = await run_skill(Editor(), "quick", options)

    assert limits == replace(AGENT_LIMITS, timeout_seconds=2)
    assert settings == ModelSettings(max_output_tokens=16)
    assert timeout == pytest.approx(2, abs=1)


async def test_run_options_that_set_one_setting_keep_the_others() -> None:
    options = RunOptions(model_settings=ModelSettings(max_output_tokens=16))

    limits, settings, _ = await run_skill(Editor(), "quick", options)

    assert limits == replace(AGENT_LIMITS, timeout_seconds=5)
    assert settings == ModelSettings(max_output_tokens=16)


async def test_run_options_apply_to_one_run_only() -> None:
    editor = Editor()
    options = RunOptions(model_settings=ModelSettings(max_output_tokens=16))
    await run_skill(editor, "edit", options)

    _, settings, _ = await run_skill(editor, "edit")

    assert settings == ModelSettings(max_output_tokens=512)


async def test_run_accepts_options_too() -> None:
    editor = Editor()
    editor.model = ScriptedModel(["Done."])
    options = RunOptions(limits=RunLimits(timeout_seconds=2))

    result = await Runtime().run(
        editor, skill="edit", arguments={"text": "Hi"}, options=options
    )

    assert generation_state(result.value).limits == replace(
        AGENT_LIMITS, timeout_seconds=2
    )
