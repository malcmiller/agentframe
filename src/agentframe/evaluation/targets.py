"""Targets: what an evaluation runs each case against."""

import copy
from typing import Any

from pydantic import ValidationError

from agentframe.authoring.agent import Agent
from agentframe.authoring.definitions import SkillDefinition
from agentframe.errors import DefinitionError, describe_validation_error
from agentframe.evaluation.cases import Case
from agentframe.runtime.limits import DEFAULT_LIMITS
from agentframe.runtime.options import RunOptions, merge_settings
from agentframe.runtime.runtime import RunResult, Runtime
from agentframe.security.principal import Principal


class LocalSkill:
    """Runs each case's ``arguments`` through one skill with ``Runtime.run``.

    Every case goes through the full runtime pipeline: argument validation,
    permissions, authorization and execution policies, and limits.

    Example::

        target = LocalSkill(Assistant(model=model), skill="answer")

    Args:
        agent: Instance of an ``@agent``-decorated class.
        skill: ID of the skill to run.
        runtime: The runtime to run it in, with its services and policies.
            ``None`` creates a default ``Runtime()``.
        principal: Verified caller identity for every case, supplied by
            trusted code. The harness never invents one, so a skill that
            requires permissions fails without it.
        options: Limits and model settings for every case's run.

    Raises:
        DefinitionError: The agent is not decorated with ``@agent`` or has no
            skill with this ID.
    """

    def __init__(
        self,
        agent: Agent,
        *,
        skill: str,
        runtime: Runtime | None = None,
        principal: Principal | None = None,
        options: RunOptions | None = None,
    ) -> None:
        definition = type(agent).definition()
        skill_definition = definition.skills.get(skill)
        if skill_definition is None:
            raise DefinitionError(
                f"Agent '{definition.name}' has no skill '{skill}' to evaluate."
            )
        self._agent = agent
        self._skill: SkillDefinition = skill_definition
        self._runtime = runtime or Runtime()
        self._principal = principal
        self._options = options

    @property
    def token_ceiling(self) -> int:
        """The most tokens one case's run may use: its resolved ``max_total_tokens``.

        Resolved like the run's own limits: library defaults, then the agent,
        the skill and ``options``. An evaluation-wide budget holds this much
        for each case before it starts.
        """
        options = self._options or RunOptions()
        limits = merge_settings(
            DEFAULT_LIMITS, self._agent.limits, self._skill.limits, options.limits
        )
        assert limits.max_total_tokens is not None, "DEFAULT_LIMITS sets it"
        return limits.max_total_tokens

    def check_case(self, case: Case[Any]) -> Case[Any]:
        """Check that ``case`` can run against this skill, before any case runs.

        Args:
            case: The case to check.

        Returns:
            The case, with ``expected`` (when set) validated and converted to
            the skill's return type, so scorers compare like with like; for
            example, a JSONL object becomes the skill's Pydantic model.

        Raises:
            DefinitionError: The case has ``text`` instead of ``arguments``,
                or its ``expected`` is not a valid skill result.
        """
        if case.arguments is None:
            raise DefinitionError(
                f"Case '{case.id}' has text, but skill '{self._skill.id}' "
                "needs arguments."
            )
        if case.expected is None:
            return case
        try:
            expected = self._skill.return_adapter.validate_python(case.expected)
        except ValidationError as error:
            raise DefinitionError(
                f"Case '{case.id}' expected value is not a valid result of skill "
                f"'{self._skill.id}': {describe_validation_error(error)}"
            ) from None
        return Case(
            id=case.id,
            arguments=case.arguments,
            expected=expected,
            tags=case.tags,
        )

    async def run(self, case: Case[Any]) -> RunResult[Any]:
        """Run the skill with the case's arguments.

        The skill gets a copy of the arguments, so it cannot change the
        dataset between runs.

        Raises:
            Exception: Whatever ``Runtime.run`` raises, unchanged.
        """
        return await self._runtime.run(
            self._agent,
            skill=self._skill.id,
            arguments=copy.deepcopy(dict(case.arguments or {})),
            principal=self._principal,
            options=self._options,
        )
