"""The Agent base class."""

from collections.abc import Callable, Iterable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar

from agentframe.authoring.definitions import (
    AgentDefinition,
    ToolDefinition,
    tools_by_name,
)
from agentframe.errors import DefinitionError
from agentframe.models.base import Model, ModelSettings
from agentframe.runtime.limits import RunLimits
from agentframe.runtime.options import RunOptions
from agentframe.security.principal import Principal

if TYPE_CHECKING:
    # Only for annotations: both modules import this one.
    from agentframe.runtime.routing import SkillRouter
    from agentframe.runtime.runtime import Runtime

DEFINITION_ATTRIBUTE = "__agentframe_definition__"


class Agent:
    """Base class for agents: declare skills with @skill, describe it with @agent.

    Args:
        model: The model ``ctx.generate`` calls. Model-free agents omit it.
            The application owns the model and closes it.
        tools: ``@tool`` functions this agent's ``ctx.generate`` calls may
            select. Registering grants nothing by itself: each call names the
            tools it offers the model. Tools are never advertised as skills.
        router: Chooses the skill for ``ask`` prompts that no single
            ``str`` skill takes, for example ``ModelRouter(model)``. Used
            only by ``ask``: an ``AgentApp`` takes its own ``router=``.

    Raises:
        DefinitionError: A tool is not decorated with ``@tool``, or two tools
            share a name.

    Attributes:
        limits: Default execution limits for this agent's skills. Fields it
            sets override the library defaults; fields a skill or run sets
            override it in turn.
        instructions: Trusted instructions sent as the first ``system`` message
            of every ``ctx.generate`` call. Never put untrusted content here.
        model_settings: Default settings for this agent's model calls, merged
            field by field like ``limits``.
        model: The model passed to the constructor, or ``None``.
        tools: The registered tools by name, read-only.
        router: The router passed to the constructor, or ``None``.

    Skills cannot be methods named like ``Agent``'s own attributes, such as
    ``invoke`` or ``ask``; give the method another name and pass the skill
    ID, for example ``@skill("ask")``.
    """

    limits: ClassVar[RunLimits] = RunLimits()
    instructions: ClassVar[str | None] = None
    model_settings: ClassVar[ModelSettings] = ModelSettings()
    # Class defaults keep subclasses that do not call super().__init__() working.
    model: Model | None = None
    tools: Mapping[str, ToolDefinition] = MappingProxyType({})
    router: "SkillRouter | None" = None

    def __init__(
        self,
        *,
        model: Model | None = None,
        tools: Iterable[Callable[..., Any]] = (),
        router: "SkillRouter | None" = None,
    ) -> None:
        self.model = model
        self.tools = tools_by_name(tools)
        self.router = router

    async def invoke(
        self,
        skill: str,
        /,
        *,
        runtime: "Runtime | None" = None,
        principal: Principal | None = None,
        options: RunOptions | None = None,
        **arguments: Any,
    ) -> Any:
        """Run one of this agent's skills and return its validated value.

        A shortcut for ``Runtime().invoke(agent, skill=..., arguments=...)``,
        with the same validation, permissions, limits and policy checks. A
        skill argument named ``runtime``, ``principal`` or ``options`` cannot
        be passed this way; use ``Runtime.invoke`` for it.

        Args:
            skill: ID of the skill to run.
            runtime: The runtime to run in, with the application's services,
                directory, authorization and policies. ``None`` uses a new
                ``Runtime()``, which has none of them.
            principal: Verified caller identity supplied by trusted code.
            options: Limits and model settings for this run only.
            **arguments: The skill's business arguments.

        Returns:
            The skill's validated value.

        Raises:
            AgentFrameError: The same errors as ``Runtime.run``, for example
                ``InputValidationError`` for an unknown skill.
        """
        # Imported here because the runtime module imports this one. A new
        # Runtime per call is cheap and shares no state between calls.
        from agentframe.runtime.runtime import Runtime

        runtime = runtime or Runtime()
        return await runtime.invoke(
            self, skill=skill, arguments=arguments, principal=principal, options=options
        )

    async def ask(
        self,
        prompt: str,
        /,
        *,
        router: "SkillRouter | None" = None,
        runtime: "Runtime | None" = None,
        principal: Principal | None = None,
        options: RunOptions | None = None,
    ) -> Any:
        """Answer a plain prompt with one of this agent's skills.

        The skill is chosen without guessing:

        1. If exactly one skill's only business parameter is a ``str``
           (``ctx``, ``Inject[T]`` and ``Remote`` parameters don't count),
           that skill gets the prompt. No model is called, even when there
           is a router.
        2. Otherwise ``router``, else the agent's own router, chooses the
           skill and its arguments among those the principal may run, like
           an ``AgentApp`` routes free text. Its model calls count toward
           the run's limits, and routing runs under the agent's limits.
        3. Otherwise ``DefinitionError`` lists the agent's skills.

        The chosen skill then runs through the full ``Runtime`` pipeline.

        Args:
            prompt: The caller's text.
            router: Chooses the skill, for example ``ModelRouter(model)``.
                ``None`` uses the agent's ``router``.
            runtime: The runtime to run in. ``None`` uses a new ``Runtime()``.
            principal: Verified caller identity supplied by trusted code.
            options: Limits and model settings for the chosen skill's run.

        Returns:
            The chosen skill's validated value.

        Raises:
            InputValidationError: The prompt is blank, or the router chose a
                skill the principal may not run.
            DefinitionError: No skill could be chosen without guessing.
            ClarificationNeededError: The router asked a question instead.
            AgentFrameError: The same errors as ``Runtime.run``.
        """
        # Imported here because these modules import this one.
        from agentframe.runtime.ask import ask
        from agentframe.runtime.runtime import Runtime

        return await ask(
            self,
            prompt,
            router=router or self.router,
            runtime=runtime or Runtime(),
            principal=principal,
            options=options,
        )

    @classmethod
    def definition(cls) -> AgentDefinition:
        """Return the validated definition created by ``@agent``.

        Raises:
            DefinitionError: If this exact class was not decorated with ``@agent``.
        """
        # Look only at this class so an undecorated subclass cannot silently
        # reuse its parent's definition and skills.
        definition = cls.__dict__.get(DEFINITION_ATTRIBUTE)
        if not isinstance(definition, AgentDefinition):
            raise DefinitionError(
                f"Agent '{cls.__name__}' is not decorated with @agent"
            )
        return definition
