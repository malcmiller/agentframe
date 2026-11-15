"""Per-run model generation behind ``RunContext.generate``.

Includes the tool loop and retries of transient provider failures. Token
accounting is in ``runtime.accounting``, token estimates and limit checks in
``runtime.tokens``, and reading the model's final answer in
``runtime.model_output``.
"""

import asyncio
import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from agentframe.authoring.services import ServiceScope
from agentframe.errors import (
    DefinitionError,
    IncompleteOutputError,
    ModelError,
    ProviderError,
    TimeoutLimitExceededError,
    UnsupportedCapabilityError,
)
from agentframe.models.base import (
    Message,
    Model,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    ModelSettings,
    OutputSchema,
)
from agentframe.runtime.accounting import UNKNOWN_USAGE, RunAccounting
from agentframe.runtime.limits import RunLimits
from agentframe.runtime.model_output import (
    TRUNCATED_MESSAGE,
    output_schema,
    text_from,
    validate_output,
)
from agentframe.runtime.tokens import (
    check_fits_model,
    estimate_input_tokens,
    estimate_output_tokens,
    with_estimates,
)
from agentframe.runtime.tools import (
    assistant_message,
    is_permitted,
    run_tool_call,
    tool_message,
    tool_spec,
)
from agentframe.security.execution_policy import ExecutionPolicy, enforce_checks
from agentframe.usage import FinishReason

if TYPE_CHECKING:
    # Imported only for type checking: these modules import this one.
    from agentframe.authoring.context import RunContext
    from agentframe.authoring.definitions import SkillDefinition, ToolDefinition
    from agentframe.orchestration.remote_tools import RemoteTool
    from agentframe.security.authorization import ToolAuthorizationPolicy

INITIAL_BACKOFF_SECONDS = 0.5
"""Longest wait before the first retry when the provider gives no hint."""

MAX_BACKOFF_SECONDS = 8.0
"""Longest wait before any retry when the provider gives no hint."""

NO_REMOTE_TOOLS: "Mapping[str, RemoteTool]" = MappingProxyType({})


@dataclass
class GenerationState:
    """One run's model, effective settings, accounting, tools and services.

    The runtime creates a fresh state for every run, so concurrent runs never
    share usage totals or call counts.

    Attributes:
        model: The agent's model, or ``None`` when it has none.
        instructions: The agent's shared trusted instructions, if any.
        settings: Effective settings for every model call in this run.
        limits: Effective limits for this run.
        services: The run's services, for the tools' ``Inject[T]`` parameters.
        accounting: Tokens, model calls and tool calls this run has used so far.
        tools: The agent's registered tools by name.
        tool_authorization: Application policy checked before every tool
            call, or ``None`` for ``requires_permissions`` checks only.
        skill: The running skill, or ``None`` while routing, before one is
            selected. Execution policy checks receive it.
        policy: Execution policy whose model-request checks run before every
            model request and whose tool checks run before every tool call,
            or ``None`` for no checks.
    """

    model: Model | None
    instructions: str | None
    settings: ModelSettings
    limits: RunLimits
    services: ServiceScope
    accounting: RunAccounting = field(default_factory=RunAccounting)
    tools: "Mapping[str, ToolDefinition]" = field(
        default_factory=lambda: MappingProxyType({})
    )
    tool_authorization: "ToolAuthorizationPolicy | None" = None
    skill: "SkillDefinition | None" = None
    policy: ExecutionPolicy | None = None

    async def generate_text(
        self,
        ctx: "RunContext",
        text: str | Sequence[Message],
        instructions: str | None,
        tools: Iterable[Callable[..., Any]] = (),
        remote_tools: "Mapping[str, RemoteTool]" = NO_REMOTE_TOOLS,
    ) -> str:
        """Send ``text`` to the model and return its complete answer.

        When ``tools`` or ``remote_tools`` are given, the model's tool calls
        are run and their results sent back until the model answers (see
        ``_generate``).

        Args:
            ctx: The run's context: its deadline, and what tools receive.
            text: Untrusted content, sent as a ``user`` message, or untrusted
                ``user`` and ``assistant`` messages sent in order.
            instructions: Trusted call-specific instructions, sent after the
                agent's own instructions as a separate ``system`` message.
            tools: Registered ``@tool`` functions to offer the model.
            remote_tools: Remote skill catalog entries to offer the model,
                bound to this run, by tool name.

        Raises:
            DefinitionError: A selected tool is not registered on the agent
                or is selected twice, a remote tool has the same name as a
                selected tool, or the model reports no context window and no
                ``max_input_tokens`` is set.
            UnsupportedCapabilityError: The agent has no model, or the model
                lacks something the request needs.
            TokenLimitExceededError: A call does not fit the input limit,
                the context window or the run's remaining tokens.
            ModelCallLimitExceededError: The run has made ``max_model_calls``.
            ToolCallLimitExceededError: The run has made ``max_tool_calls``.
            ModelRefusalError: The model refused, or a content filter blocked it.
            IncompleteOutputError: The output was cut short or empty. When it
                stopped at the output token limit, ``partial_text`` holds the
                text received so far.
            ProviderError: The provider call failed, and was not transient,
                used every retry, or would have to wait past the deadline.
            OutputValidationError: A tool returned an invalid value.
            TimeoutLimitExceededError: The run's deadline passed during a
                model or tool call.
        """
        response = await self._generate(
            ctx, text, instructions, None, tools, remote_tools
        )
        if response.finish_reason is FinishReason.LENGTH and response.text:
            # Only text keeps what arrived before the limit, so a skill can
            # catch the error and choose to use it. Structured output never does.
            raise IncompleteOutputError(TRUNCATED_MESSAGE, partial_text=response.text)
        return text_from(response)

    async def generate_output[T: BaseModel](
        self,
        ctx: "RunContext",
        text: str | Sequence[Message],
        instructions: str | None,
        output: type[T],
        tools: Iterable[Callable[..., Any]] = (),
        remote_tools: "Mapping[str, RemoteTool]" = NO_REMOTE_TOOLS,
    ) -> T:
        """Send ``text`` to the model and return its reply validated as ``output``.

        The model is asked for JSON matching ``output``'s schema. Invalid
        output is never repaired or re-requested. With ``tools``, the final
        answer after the tool calls is the one validated. Besides the errors
        below, this raises everything ``generate_text`` raises.

        Args:
            ctx: The run's context (see ``generate_text``).
            text: Untrusted content, sent as a ``user`` message, or untrusted
                ``user`` and ``assistant`` messages sent in order.
            instructions: Trusted call-specific instructions (see
                ``generate_text``).
            output: The Pydantic model class the reply must validate as.
            tools: Registered ``@tool`` functions to offer the model.
            remote_tools: Remote skill catalog entries to offer the model
                (see ``generate_text``).

        Raises:
            DefinitionError: ``output`` is not a ``BaseModel`` subclass, or is
                a ``RootModel``.
            UnsupportedCapabilityError: The agent has no model, or the model
                does not support structured output.
            OutputValidationError: The reply is not valid JSON or does not
                match ``output``. The message lists field locations only.
        """
        schema = output_schema(output)
        response = await self._generate(
            ctx, text, instructions, schema, tools, remote_tools
        )
        return validate_output(text_from(response), output)

    async def _generate(
        self,
        ctx: "RunContext",
        text: str | Sequence[Message],
        instructions: str | None,
        output: OutputSchema | None,
        tools: Iterable[Callable[..., Any]],
        remote_tools: "Mapping[str, RemoteTool]",
    ) -> ModelResponse:
        """Run the model and the tools it calls within the run's deadline.

        Returns:
            The model's final response: the first one that calls no tools.
        """
        selected_tools = self.select_tools(tools)
        check_unique_tool_names(selected_tools, remote_tools)
        if self.model is None:
            raise UnsupportedCapabilityError("This agent has no model configured")
        messages = build_messages(self.instructions, instructions, text)
        try:
            async with asyncio.timeout_at(ctx.deadline) as timeout:
                return await self._tool_loop(
                    self.model, ctx, messages, output, selected_tools, remote_tools
                )
        except TimeoutError:
            if not timeout.expired():
                raise
            raise TimeoutLimitExceededError(
                "The run's timeout expired during a model or tool call"
            ) from None

    async def _tool_loop(
        self,
        model: Model,
        ctx: "RunContext",
        messages: tuple[Message, ...],
        output: OutputSchema | None,
        tools: Mapping[str, "ToolDefinition"],
        remote_tools: "Mapping[str, RemoteTool]",
    ) -> ModelResponse:
        """Call the model, run the tools it asks for, and repeat until it answers.

        Each model call is an ordinary call: it counts toward
        ``max_model_calls`` and the token budget. Tool calls in one response
        run one at a time, in the model's order, and each counts toward
        ``max_tool_calls`` before it runs. The loop ends because those limits
        are finite. A tool call, local or remote, is never retried.

        Tools and remote entries the caller lacks a required permission for
        are left out of the request, and every call is authorized again,
        then passes the execution policy's tool checks, before it runs.
        Every request, including each continuation, passes the execution
        policy's model-request checks before it is sent.
        """
        specs = tuple(
            tool_spec(tool)
            for tool in tools.values()
            if is_permitted(tool, ctx.principal)
        ) + tuple(
            remote.spec()
            for remote in remote_tools.values()
            if is_permitted(remote.entry, ctx.principal)
        )
        while True:
            request = ModelRequest(
                messages=messages, settings=self.settings, output=output, tools=specs
            )
            await self._check_request(request, ctx)
            response = await self._call_with_retries(model, request, ctx.deadline)
            # Checked before the finish reason: a tool-call response has no text.
            if not response.tool_calls:
                return response
            messages += (assistant_message(response),)
            for call in response.tool_calls:
                self.accounting.count_tool_call(self.limits)
                # Looked up by name: the model's name never reaches a binding
                # on its own.
                remote = remote_tools.get(call.name)
                if remote is not None:
                    result = await remote.run(call.arguments)
                else:
                    result = await run_tool_call(
                        call,
                        tools,
                        self.services,
                        ctx,
                        self.tool_authorization,
                        self.policy,
                        self.skill,
                    )
                messages += (tool_message(call, result),)

    async def _check_request(self, request: ModelRequest, ctx: "RunContext") -> None:
        """Run the model-request checks once for a logical request.

        Checked before any tokens are reserved, and not again when a
        transient failure retries the identical request.
        """
        if self.policy is None:
            return
        await enforce_checks(
            self.policy.model_request_checks,
            self.skill,
            ctx.principal,
            request,
            ctx.deadline,
        )

    def select_tools(
        self, functions: Iterable[Callable[..., Any]]
    ) -> Mapping[str, "ToolDefinition"]:
        """Return the definitions of ``functions`` by name, in the order given.

        Only tools registered on the agent can be selected; registering a
        tool never offers it to a model on its own.

        Raises:
            DefinitionError: A function is not a tool registered on the agent,
                or is selected more than once.
        """
        by_function = {tool.function: tool for tool in self.tools.values()}
        selected: dict[str, ToolDefinition] = {}
        for function in functions:
            definition = by_function.get(function)
            # Tool names are author-defined, never caller data.
            name = getattr(function, "__name__", repr(function))
            if definition is None:
                raise DefinitionError(
                    f"Tool '{name}' is not registered on this agent; pass it "
                    "to the agent's tools=[...]"
                )
            if definition.name in selected:
                raise DefinitionError(f"Tool '{name}' is selected more than once")
            selected[definition.name] = definition
        return MappingProxyType(selected)

    async def _call_with_retries(
        self, model: Model, request: ModelRequest, deadline: float
    ) -> ModelResponse:
        """Call the model, retrying transient provider failures with backoff.

        Every retry is a new call through ``_call``, so it reserves tokens and
        counts toward ``max_model_calls``. Other errors, including refusals and
        incomplete output, are raised at once. When the wait before a retry
        would reach the deadline, the provider error is raised instead.
        """
        max_retries = self.limits.max_model_retries
        assert max_retries is not None  # Set by defaults.
        for retry in range(max_retries):
            try:
                return await self._call(model, request)
            except ProviderError as error:
                if not error.transient:
                    raise
                delay = backoff_seconds(error, retry)
                if asyncio.get_running_loop().time() + delay >= deadline:
                    raise
            await wait_before_retry(delay)
        return await self._call(model, request)

    async def _call(self, model: Model, request: ModelRequest) -> ModelResponse:
        """Reserve tokens, call the model, then settle the call's actual usage."""
        capabilities = await model.capabilities()
        check_supports(request, capabilities)
        input_estimate = estimate_input_tokens(request)
        max_output_tokens = request.settings.max_output_tokens
        assert max_output_tokens is not None  # ModelRequest requires every setting.
        check_fits_model(input_estimate, max_output_tokens, self.limits, capabilities)
        reservation = input_estimate + max_output_tokens
        self.accounting.reserve(reservation, self.limits)
        try:
            response = await model.generate(request)
        except UnsupportedCapabilityError:
            self.accounting.release(reservation)  # Raised before anything was sent.
            raise
        except ModelError as error:
            self.accounting.settle(reservation, error.usage or UNKNOWN_USAGE)
            raise
        except BaseException:
            # Cancelled or failed mid-call: tokens may have been spent.
            self.accounting.settle(reservation, UNKNOWN_USAGE)
            raise
        self.accounting.settle(
            reservation,
            with_estimates(
                response.usage, input_estimate, estimate_output_tokens(response)
            ),
        )
        return response


def check_unique_tool_names(
    tools: Mapping[str, "ToolDefinition"], remote_tools: Mapping[str, "RemoteTool"]
) -> None:
    """Refuse a remote tool named like a selected ``@tool``, before any model call.

    Raises:
        DefinitionError: A name is used by both.
    """
    for name, remote in remote_tools.items():
        if name in tools:
            raise DefinitionError(
                f"Remote skill '{remote.entry.name}' and tool '{name}' both "
                f"become the model tool '{name}'; pass only one of them."
            )


def backoff_seconds(error: ProviderError, retry: int) -> float:
    """Return how long to wait before retry number ``retry`` (counting from 0).

    The provider's ``retry_after`` hint is used when present. Otherwise the
    wait is a random time up to an exponentially growing bound ("full
    jitter"), so many failed runs do not all retry at the same moment.
    """
    if error.retry_after is not None:
        return error.retry_after
    bound = min(MAX_BACKOFF_SECONDS, INITIAL_BACKOFF_SECONDS * 2**retry)
    return random.uniform(0, bound)


async def wait_before_retry(seconds: float) -> None:
    """Wait ``seconds`` before retrying a model call. Tests replace this."""
    await asyncio.sleep(seconds)


def check_supports(request: ModelRequest, capabilities: ModelCapabilities) -> None:
    """Check that the model supports what ``request`` needs, before it is sent.

    Raises:
        UnsupportedCapabilityError: The request asks for structured output or
            offers tools, and the model does not report support for it.
    """
    if request.output is not None and not capabilities.structured_output:
        raise UnsupportedCapabilityError("The model does not support structured output")
    if request.tools and not capabilities.tools:
        raise UnsupportedCapabilityError("The model does not support tool calling")


def build_messages(
    agent_instructions: str | None,
    call_instructions: str | None,
    text: str | Sequence[Message],
) -> tuple[Message, ...]:
    """Put trusted instructions in ``system`` messages, then the untrusted ``text``.

    ``text`` is one ``user`` message, or a conversation of messages sent as
    given after the instructions. Untrusted text is never merged into
    instructions. This keeps the roles separate; it does not guarantee
    resistance to prompt injection.
    """
    messages = [
        Message(role="system", content=instructions)
        for instructions in (agent_instructions, call_instructions)
        if instructions
    ]
    if isinstance(text, str):
        messages.append(Message(role="user", content=text))
    else:
        messages.extend(text)
    return tuple(messages)
