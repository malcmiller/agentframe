"""Measure the planner's choices: an evaluation of its ``propose`` skill.

Each case is a goal and a reference plan. The scorers compare only a plan's
structure: which entries it calls, and which calls wait for which. Never step
IDs, argument values or wording, since a model can write the same plan many
ways and plans differ from run to run. Run it with a real model to measure
the model; see docs/guides/orchestration.md#measure-the-planner.

The research and writer agents must be running, since the catalog comes from
their Agent Cards. Pass a ``Runtime`` whose directory finds them by name and
whose ``remotes=`` hold the keys to read their cards with.
"""

from collections import Counter

from pydantic import JsonValue

from agentframe import (
    Model,
    ModelSettings,
    Plan,
    PlanStep,
    Principal,
    RunLimits,
    RunOptions,
    Runtime,
)
from agentframe.evaluation import Case, Evaluation, LocalSkill
from examples.multi_agent.planner import Planner

FIND_SOURCES = "research-agent.find_sources"
BRIEF = "research-agent.brief"
WRITE_POST = "writer-agent.write_post"


def step(step_id: str, use: str, **arguments: JsonValue) -> PlanStep:
    return PlanStep(id=step_id, use=use, arguments=arguments)


GOALS = [
    Case(
        id="sources",
        arguments={"goal": "List sources about tides."},
        expected=Plan(
            steps=[step("tides", FIND_SOURCES, topic="tides")], result="tides"
        ),
    ),
    Case(
        id="brief",
        arguments={"goal": "Get a short research brief about waves, nothing else."},
        expected=Plan(
            steps=[step("waves", BRIEF, question="What about waves?")], result="waves"
        ),
    ),
    Case(
        id="two_topics",
        arguments={"goal": "Find sources about tides and, separately, about waves."},
        expected=Plan(
            steps=[
                step("tides", FIND_SOURCES, topic="tides"),
                step("waves", FIND_SOURCES, topic="waves"),
            ],
            result="waves",
        ),
    ),
    Case(
        id="post_from_sources",
        arguments={
            "goal": "Find sources about weather, then write a post titled "
            "Weather that lists those sources."
        },
        expected=Plan(
            steps=[
                step("weather", FIND_SOURCES, topic="weather"),
                step("post", WRITE_POST, title="Weather", sources={"$ref": "weather"}),
            ],
            result="post",
        ),
    ),
    Case(
        id="post_with_brief",
        arguments={
            "goal": "Write a post titled Tides from sources about tides, with a "
            "short research brief about tides as its notes."
        },
        expected=Plan(
            steps=[
                step("tides", FIND_SOURCES, topic="tides"),
                step("brief", BRIEF, question="What about tides?"),
                step(
                    "post",
                    WRITE_POST,
                    title="Tides",
                    sources={"$ref": "tides"},
                    notes={"$ref": "brief"},
                ),
            ],
            result="post",
        ),
    ),
    Case(
        id="post_only",
        arguments={"goal": "Write a post titled Hello that lists no sources."},
        expected=Plan(
            steps=[step("post", WRITE_POST, title="Hello", sources=[])], result="post"
        ),
    ),
]


def same_entries(case: Case[Plan], plan: Plan) -> bool:
    """Pass when the plan calls the same entries as the reference, as often."""
    assert case.expected is not None
    return calls(plan) == calls(case.expected)


def same_dependencies(case: Case[Plan], plan: Plan) -> bool:
    """Pass when the same entries wait for the same entries as in the reference."""
    assert case.expected is not None
    return waits(plan) == waits(case.expected)


def calls(plan: Plan) -> Counter[str]:
    return Counter(step.use for step in plan.steps)


def waits(plan: Plan) -> Counter[tuple[str, str]]:
    """Count each (entry, entry it waits for) pair, from references and ``after``."""
    uses = {step.id: step.use for step in plan.steps}
    return Counter(
        (step.use, uses[needed])
        for step in plan.steps
        for needed in referenced_steps(step.arguments) | set(step.after)
        if needed in uses
    )


def referenced_steps(value: object) -> set[str]:
    """Return the step IDs that ``{"$ref": ...}`` objects anywhere in ``value`` name."""
    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str):
            return {reference.split(".")[0]}
        return set().union(*map(referenced_steps, value.values()))
    if isinstance(value, list):
        return set().union(*map(referenced_steps, value))
    return set()


def create_planner_evaluation(model: Model, runtime: Runtime) -> Evaluation:
    """Check which entries ``propose`` plans to call for each goal, and in what order.

    Args:
        model: The planning model, with structured output.
        runtime: Runs ``propose``; its directory and ``remotes=`` must reach
            the research and writer agents.
    """
    target = LocalSkill(
        Planner(model=model),
        skill="propose",
        runtime=runtime,
        # Placeholder caller: ``posts:write`` shows it the writer's skill.
        principal=Principal(id="evaluator", permissions=frozenset({"posts:write"})),
        options=RunOptions(
            limits=RunLimits(max_total_tokens=8_000),
            # Reasoning deployments count reasoning tokens toward the output cap.
            model_settings=ModelSettings(max_output_tokens=2_048),
        ),
    )
    return Evaluation(
        cases=GOALS,
        target=target,
        scorers=[same_entries, same_dependencies],
        max_total_tokens=60_000,
    )
