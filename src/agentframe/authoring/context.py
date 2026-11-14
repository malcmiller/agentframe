"""Per-run context passed to skills."""

from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field
from typing import Any, overload

from pydantic import BaseModel

from agentframe.discovery.remote import Remote
from agentframe.errors import DefinitionError
from agentframe.orchestration.catalog import SkillCatalog, build_catalog
from agentframe.runtime.generation import GenerationState
from agentframe.runtime.remotes import RunRemotes
from agentframe.security.principal import Principal


@dataclass(frozen=True)
class RunContext:
    """Per-run information passed to every skill. Never shared between runs.

    Attributes:
        invocation_id: Library-generated identifier of this run.
        principal: Verified caller identity, or ``None`` when none was supplied.
        deadline: Event-loop time (``loop.time()``) when the run times out.
        hops: The run's depth in a chain of calls between agents: 0 when
            started locally or by a caller that sent no depth, and one more
            than the calling run's depth when another agent sent it.
    """

    invocation_id: str
    principal: Principal | None
    deadline: float
    # Private per-run model state; the context is frozen but its accounting
    # must change as the run makes model calls.
    _generation: GenerationState = field(repr=False, compare=False)
    hops: int = 0
    # Private per-run bindings to remote agents; None without a directory.
    _remotes: RunRemotes | None = field(default=None, repr=False, compare=False)

    @overload
    async def generate(
        self,
        text: str,
        *,
        instructions: str | None = None,
        tools: Iterable[Callable[..., Any]] = (),
    ) -> str: ...

    @overload
    async def generate[T: BaseModel](
        self,
        text: str,
        *,
        instructions: str | None = None,
        output: type[T],
        tools: Iterable[Callable[..., Any]] = (),
    ) -> T: ...

    async def generate(
        self,
        text: str,
        *,
        instructions: str | None = None,
        output: type[BaseModel] | None = None,
        tools: Iterable[Callable[..., Any]] = (),
    ) -> str | BaseModel:
        """Generate text, or a validated ``output`` instance, with the agent's model.

        The agent's ``instructions`` and then ``instructions`` are sent as
        trusted ``system`` messages; ``text`` is sent as a separate ``user``
        message and never merged into them. Keeping them separate does not
        guarantee resistance to prompt injection.

        Args:
            text: Untrusted content for the model, such as caller input.
            instructions: Trusted instructions for this call only. Never put
                untrusted content here.
            output: A Pydantic ``BaseModel`` subclass. When given, the model
                is asked for JSON matching its schema and the reply is
                validated into an instance. Invalid output is never repaired
                or re-requested.
            tools: ``@tool`` functions to offer the model, each registered on
                the agent and selected once. Omitted or empty offers no
                tools; registered tools are never offered unless selected
                here. The model's tool calls run one at a time, in its
                order, and their results are sent back until it answers.
                A call naming a tool that was not offered, or with invalid
                arguments, runs nothing; the model is told what was wrong.
                Tools whose ``requires_permissions`` the caller lacks are
                not offered, and every call is authorized before it runs.

        Returns:
            The model's complete answer, or an ``output`` instance when
            ``output`` is given. With tools, the answer after the tool calls.

        Raises:
            DefinitionError: ``output`` is not a ``BaseModel`` subclass, or is
                a ``RootModel``, or a selected tool is not registered on the
                agent or is selected twice. Raised before anything is sent.
            UnsupportedCapabilityError: The agent has no model configured, or
                the model lacks something the request needs, such as
                structured output or tool calling. Raised before anything is
                sent.
            OutputValidationError: The structured reply is not valid JSON or
                does not match ``output``, or a tool returned a value that
                does not match its return annotation.
            ModelRefusalError: The model refused, or a content filter blocked
                its output.
            IncompleteOutputError: The output was cut short at the token
                limit or was empty. Partial text or objects are never returned;
                for text cut short at the limit, the error's ``partial_text``
                holds what arrived, and a skill may catch it and use it.
            ProviderError: The model provider call failed.
            PermissionDeniedError: The model called a tool the caller lacks
                a required permission for, or the runtime's tool
                authorization policy denied a call. The model is never told.
                Also raised, before anything is sent, when an execution
                policy model-request check blocks a request.
            PolicyUnavailableError: The tool authorization policy or an
                execution policy model-request check raised an error or did
                not answer before the run's deadline.
            LimitExceededError: A model call does not fit the run's limits,
                the run has made ``max_tool_calls`` tool calls, or the run's
                timeout expired during a model or tool call.

        Exceptions raised by a tool propagate unchanged and fail the run;
        their text is never sent to the model.
        """
        if output is None:
            return await self._generation.generate_text(self, text, instructions, tools)
        return await self._generation.generate_output(
            self, text, instructions, output, tools
        )

    async def remote_skills(
        self,
        *remotes: Remote[Any],
        agents: Collection[str] | None = None,
        skill: str | None = None,
        tags: Collection[str] = (),
        ask: bool = False,
    ) -> SkillCatalog:
        """Return a catalog of the remote skills this run may offer a model.

        With no ``remotes``, every agent the directory's sources list is
        considered: the registry is the allowlist. An agent is included only
        when its card can be fetched and is valid (with the directory's
        usual checks and cache, at most eight fetches at a time) and its
        card's security is open or met by its ``remotes=`` entry. Any other
        agent, and any source that cannot list its agents, is skipped with
        one WARNING log naming only the agent (or the source's position and
        class) and the reason. An empty catalog is valid. When the run is
        hosted by an ``AgentApp``, the agent itself is left out (matched by
        its advertised A2A URL); a local run leaves nothing out.

        With ``remotes``, the skill's own ``Remote`` parameters, the catalog
        holds exactly those agents, and a problem with one raises instead.

        An agent whose card declares AgentFrame's skill-call extension gives
        one entry per card skill, with its argument schema; any other agent
        gives one free-text ``ask`` entry describing it. Card text is
        untrusted: descriptions (also inside schemas) are cut to 500
        characters, at most ten tags are kept, control characters are
        removed and ``examples`` are dropped. Nothing calls a model.

        Args:
            *remotes: ``Remote`` parameters of the running skill.
            agents: Agent names to keep. ``None`` keeps every agent.
            skill: Keep only card skills with this ID.
            tags: Keep only card skills that have all of these tags. Matched
                with ``skill`` on the same card skill, as
                ``AgentDirectory.find``; an ``ask`` entry is kept when any of
                its agent's card skills matches.
            ask: Also add an ``ask`` entry for agents with the extension.

        Returns:
            The catalog, holding the run's binding to each of its agents.

        Raises:
            DefinitionError: The runtime has no directory, the directory is
                not open, a name is listed by more than one source, no
                source lists a name in ``agents`` (unless a source failed),
                ``agents`` or ``tags`` is a single string, a positional
                argument is not a ``Remote``, two entries share a name, or
                more than 20 entries remain after narrowing and skipping. A
                catalog is never truncated.
            RemoteAgentError: A ``Remote`` parameter's card has an unsafe
                or repeated skill ID, or a missing or over-8-KiB argument
                schema.
            PermissionDeniedError: An included agent is called
                ``OnBehalfOf`` the user, and the run has no signed-in user.
        """
        if self._remotes is None:
            raise DefinitionError(
                "remote_skills needs an AgentDirectory; pass directory= to "
                "Runtime or AgentApp"
            )
        return await build_catalog(
            self._remotes, remotes, agents=agents, skill=skill, tags=tags, ask=ask
        )
