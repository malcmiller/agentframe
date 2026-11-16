"""Per-run model generation behind ``RunContext.generate``.

Includes the tool loop and retries of transient provider failures. Token
accounting is in ``runtime.accounting``, token estimates and limit checks in
``runtime.tokens``, and reading the model's final answer in
``runtime.model_output``.
"""

import asyncio
import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
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
    ToolCallLimitExceededError,
    UnsupportedCapabilityError,
)
from agentframe.models.base import (
    HostedTool,
    Message,
    Model,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    ModelSettings,
    OutputSchema,
    StreamingModel,
    TextSink,
    ToolSpec,
)
from agentframe.runtime.accounting import UNKNOWN_USAGE, RunAccounting
from agentframe.runtime.limits import RunLimits
from agentframe.runtime.model_output import (
    TRUNCATED_MESSAGE,
    output_schema,
    text_from,
    validate_output,
)
from agentframe.runtime.state import newest_within
from agentframe.runtime.telemetry import (
    NO_TELEMETRY,
    Telemetry,
    error_type,
    model_attributes,
)
from agentframe.runtime.tokens import (
    check_fits_model,
    estimate_input_tokens,
    estimate_message_tokens,
    estimate_output_tokens,
    input_token_limit,
    with_estimates,
)
from agentframe.runtime.tools import (
    BoundTool,
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
    from agentframe.security.authorization import ToolAuthorizationPolicy

INITIAL_BACKOFF_SECONDS = 0.5
"""Longest wait before the first retry when the provider gives no hint."""

MAX_BACKOFF_SECONDS = 8.0
"""Longest wait before any retry when the provider gives no hint."""

NO_BOUND_TOOLS: Mapping[str, BoundTool] = MappingProxyType({})


@dataclass
class _SentText:
    """Passes text on to ``sink`` and remembers whether any was sent."""

    sink: TextSink
    started: bool = False

    async def __call__(self, text: str) -> None:
        self.started = True
        await self.sink(text)


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
        text_sink: Where ``generate_text(stream=True)`` sends answer text as
            it arrives, or ``None`` when the run's caller does not stream.
            The runtime leaves it ``None`` when output checks must see the
            whole answer first.
        telemetry: Records model and tool call spans and metrics; disabled
            unless the runtime has tracing configured.
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
    text_sink: TextSink | None = None
    telemetry: Telemetry = NO_TELEMETRY

    async def generate_text(
        self,
        ctx: "RunContext",
        text: str | Sequence[Message],
        instructions: str | None,
        tools: Iterable[Callable[..., Any]] = (),
        bound_tools: Mapping[str, BoundTool] = NO_BOUND_TOOLS,
        hosted_tools: Sequence[HostedTool] = (),
        history: Sequence[Message] = (),
        *,
        stream: bool = False,
    ) -> str:
        """Send ``text`` to the model and return its complete answer.

        When ``tools`` or ``bound_tools`` are given, the model's tool calls
        are run and their results sent back until the model answers (see
        ``_generate``).

        Args:
            ctx: The run's context: its deadline, and what tools receive.
            text: Untrusted content, sent as a ``user`` message, or untrusted
                ``user`` and ``assistant`` messages sent in order.
            instructions: Trusted call-specific instructions, sent after the
                agent's own instructions as a separate ``system`` message.
            tools: Registered ``@tool`` functions to offer the model.
            bound_tools: Remote skill catalog entries and MCP server tools
                to offer the model, bound to this run, by tool name.
            hosted_tools: Provider-run tools to offer the model. Each call
                the provider ran counts toward ``max_tool_calls``.
            history: Earlier untrusted ``user`` and ``assistant`` messages,
                oldest first, sent between the instructions and ``text``. The
                oldest are left out when they would not fit (see
                ``fit_history``).
            stream: Send the model's text to ``text_sink`` as it arrives.
                Ignored without a sink or with a model that has no
                ``stream`` method.

        Raises:
            DefinitionError: A selected tool is not registered on the agent
                or is selected twice, a remote or MCP tool has the same name
                as a selected tool, a hosted tool type is selected twice,
                ``history`` holds a message that is not a plain ``user`` or
                ``assistant`` message, or the model reports no context window and no
                ``max_input_tokens`` is set.
            UnsupportedCapabilityError: The agent has no model, or the model
                lacks something the request needs, such as a hosted tool.
            TokenLimitExceededError: A call does not fit the input limit,
                the context window or the run's remaining tokens.
            ModelCallLimitExceededError: The run has made ``max_model_calls``.
            ToolCallLimitExceededError: The run has made ``max_tool_calls``,
                before a tool call or a request offering hosted tools, or
                the provider's hosted tool calls took it past the limit.
            ModelRefusalError: The model refused, or a content filter blocked it.
            IncompleteOutputError: The output was cut short or empty. When it
                stopped at the output token limit, ``partial_text`` holds the
                text received so far.
            ProviderError: The provider call failed, and was not transient,
                used every retry, or would have to wait past the deadline.
                A call that already streamed text is never retried.
            OutputValidationError: A tool returned an invalid value.
            TimeoutLimitExceededError: The run's deadline passed during a
                model or tool call.
        """
        on_text = self.text_sink if stream else None
        response = await self._generate(
            ctx,
            text,
            instructions,
            None,
            tools,
            bound_tools,
            hosted_tools,
            history,
            on_text,
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
        bound_tools: Mapping[str, BoundTool] = NO_BOUND_TOOLS,
        hosted_tools: Sequence[HostedTool] = (),
        history: Sequence[Message] = (),
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
            bound_tools: Remote skill catalog entries and MCP server tools
                to offer the model (see ``generate_text``).
            hosted_tools: Provider-run tools to offer the model (see
                ``generate_text``).
            history: Earlier messages (see ``generate_text``).

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
            ctx,
            text,
            instructions,
            schema,
            tools,
            bound_tools,
            hosted_tools,
            history,
            None,
        )
        return validate_output(text_from(response), output)

    async def _generate(
        self,
        ctx: "RunContext",
        text: str | Sequence[Message],
        instructions: str | None,
        output: OutputSchema | None,
        tools: Iterable[Callable[..., Any]],
        bound_tools: Mapping[str, BoundTool],
        hosted_tools: Sequence[HostedTool],
        history: Sequence[Message],
        on_text: TextSink | None,
    ) -> ModelResponse:
        """Run the model and the tools it calls within the run's deadline.

        Returns:
            The model's final response: the first one that calls no tools.
        """
        selected_tools = self.select_tools(tools)
        check_unique_tool_names(selected_tools, bound_tools)
        check_history(history)
        if self.model is None:
            raise UnsupportedCapabilityError("This agent has no model configured")
        # Tools, remote entries and MCP tools the caller lacks a required
        # permission for are left out of the request.
        specs = tuple(
            tool_spec(tool)
            for tool in selected_tools.values()
            if is_permitted(tool, ctx.principal)
        ) + tuple(
            bound.spec()
            for bound in bound_tools.values()
            if is_permitted(bound.entry, ctx.principal)
        )
        try:
            async with asyncio.timeout_at(ctx.deadline) as timeout:
                if history:
                    history = await self.fit_history(
                        self.model, history, instructions, text, output, specs
                    )
                messages = build_messages(
                    self.instructions, instructions, text, history
                )
                return await self._tool_loop(
                    self.model,
                    ctx,
                    messages,
                    output,
                    specs,
                    selected_tools,
                    bound_tools,
                    tuple(hosted_tools),
                    on_text,
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
        specs: tuple[ToolSpec, ...],
        tools: Mapping[str, "ToolDefinition"],
        bound_tools: Mapping[str, BoundTool],
        hosted_tools: tuple[HostedTool, ...],
        on_text: TextSink | None,
    ) -> ModelResponse:
        """Call the model, run the tools it asks for, and repeat until it answers.

        Each model call is an ordinary call: it counts toward
        ``max_model_calls`` and the token budget. Tool calls in one response
        run one at a time, in the model's order, and each counts toward
        ``max_tool_calls`` before it runs. The loop ends because those limits
        are finite. A tool call, local, remote or MCP, is never retried.

        ``specs`` offers only the tools the caller is permitted, and every call
        is authorized again, then passes the execution policy's tool checks,
        before it runs. Every request, including each continuation, passes
        the execution policy's model-request checks before it is sent.

        Hosted tools run on the provider's side, so they bypass tool
        authorization and tool checks. A request offering them caps the
        provider at the run's remaining tool calls, and fails before sending
        when none remain; the calls the provider ran are counted after each
        response.

        With ``on_text``, every call streams, so text the model writes
        before calling tools is passed on too.
        """
        while True:
            request = ModelRequest(
                messages=messages,
                settings=self.settings,
                output=output,
                tools=specs,
                hosted_tools=hosted_tools,
                max_hosted_tool_calls=self._hosted_tool_allowance(hosted_tools),
            )
            await self._check_request(request, ctx)
            response = await self._traced_call(model, request, ctx.deadline, on_text)
            self.accounting.count_hosted_tool_calls(
                response.hosted_tool_calls, self.limits
            )
            # Checked before the finish reason: a tool-call response has no text.
            if not response.tool_calls:
                return response
            messages += (assistant_message(response),)
            for call in response.tool_calls:
                self.accounting.count_tool_call(self.limits)
                # Only declared names and the ID: arguments and results are
                # content, and an unknown name is the model's own text.
                attributes = {
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.call.id": call.id,
                }
                span_name = "execute_tool"
                if call.name in tools or call.name in bound_tools:
                    attributes["gen_ai.tool.name"] = call.name
                    span_name = f"execute_tool {call.name}"
                with self.telemetry.span(span_name, attributes=attributes):
                    # Looked up by name: the model's name never reaches a
                    # binding on its own.
                    bound = bound_tools.get(call.name)
                    if bound is not None:
                        result = await bound.run(call.arguments)
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

    def _hosted_tool_allowance(
        self, hosted_tools: tuple[HostedTool, ...]
    ) -> int | None:
        """Return the hosted tool calls a request may make, or ``None`` without any.

        Raises:
            ToolCallLimitExceededError: Hosted tools are offered, but the run
                has made ``max_tool_calls``.
        """
        if not hosted_tools:
            return None
        remaining = self.accounting.remaining_tool_calls(self.limits)
        if remaining == 0:
            raise ToolCallLimitExceededError(
                f"The run reached its limit of {self.limits.max_tool_calls} "
                "tool calls, so it cannot offer hosted tools"
            )
        return remaining

    async def fit_history(
        self,
        model: Model,
        history: Sequence[Message],
        instructions: str | None,
        text: str | Sequence[Message],
        output: OutputSchema | None,
        specs: tuple[ToolSpec, ...],
    ) -> list[Message]:
        """Return the newest whole ``history`` messages that fit the first request.

        The request must fit the input limit, the context window less the
        output cap, and the run's remaining tokens less the output cap. The
        oldest messages are left out until the rest fits, and what is kept
        starts with a ``user`` message. Only the first request is fitted:
        tool results added later may still exceed a limit and fail as usual.
        """
        max_output = self.settings.max_output_tokens
        max_total = self.limits.max_total_tokens
        assert max_output is not None and max_total is not None  # Set by defaults.
        capabilities = await model.capabilities()
        bounds = [
            input_token_limit(self.limits, capabilities),
            self.accounting.remaining_tokens(max_total) - max_output,
        ]
        if capabilities.context_window is not None:
            bounds.append(capabilities.context_window - max_output)
        without_history = ModelRequest(
            messages=build_messages(self.instructions, instructions, text),
            settings=self.settings,
            output=output,
            tools=specs,
        )
        budget = min(bounds) - estimate_input_tokens(without_history)
        return newest_within(history, budget, estimate_message_tokens)

    async def _traced_call(
        self,
        model: Model,
        request: ModelRequest,
        deadline: float,
        on_text: TextSink | None,
    ) -> ModelResponse:
        """Call the model with retries inside a ``chat`` span, and record metrics.

        One span and one duration cover the logical call, retries included;
        each retry is an event on the span.
        """
        attributes = model_attributes(model)
        model_name = attributes.get("gen_ai.request.model")
        name = f"chat {model_name}" if model_name else "chat"
        started = asyncio.get_running_loop().time()
        try:
            with self.telemetry.span(
                name,
                kind="client",
                attributes={
                    **attributes,
                    "gen_ai.request.max_tokens": request.settings.max_output_tokens,
                },
            ) as span:
                response = await self._call_with_retries(
                    model, request, deadline, on_text
                )
                span.set("gen_ai.usage.input_tokens", response.usage.input_tokens)
                span.set("gen_ai.usage.output_tokens", response.usage.output_tokens)
                span.set(
                    "gen_ai.response.finish_reasons",
                    (str(response.finish_reason),),
                )
        except BaseException as error:
            self.telemetry.record_model_call(
                model,
                seconds=asyncio.get_running_loop().time() - started,
                usage=None,
                error=error,
            )
            raise
        self.telemetry.record_model_call(
            model,
            seconds=asyncio.get_running_loop().time() - started,
            usage=response.usage,
            error=None,
        )
        return response

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
        self,
        model: Model,
        request: ModelRequest,
        deadline: float,
        on_text: TextSink | None = None,
    ) -> ModelResponse:
        """Call the model, retrying transient provider failures with backoff.

        Every retry is a new call through ``_call``, so it reserves tokens and
        counts toward ``max_model_calls``. Other errors, including refusals and
        incomplete output, are raised at once. When the wait before a retry
        would reach the deadline, the provider error is raised instead. A
        retry offering hosted tools is capped at the tool calls left after the
        failed attempt's. A call that already passed text to ``on_text`` is
        never retried: that text cannot be taken back, and a retry would send
        it again.
        """
        sent = _SentText(on_text) if on_text is not None else None
        max_retries = self.limits.max_model_retries
        assert max_retries is not None  # Set by defaults.
        for retry in range(max_retries):
            try:
                return await self._call(model, request, sent)
            except ProviderError as error:
                if not error.transient or (sent is not None and sent.started):
                    raise
                delay = backoff_seconds(error, retry)
                if asyncio.get_running_loop().time() + delay >= deadline:
                    raise
                self.telemetry.add_event("retry", {"error.type": error_type(error)})
            await wait_before_retry(delay)
            request = replace(
                request,
                max_hosted_tool_calls=self._hosted_tool_allowance(request.hosted_tools),
            )
        return await self._call(model, request, sent)

    async def _call(
        self, model: Model, request: ModelRequest, on_text: TextSink | None
    ) -> ModelResponse:
        """Reserve tokens, call the model, then settle the call's actual usage.

        Streams to ``on_text`` when given and the model can stream.
        """
        capabilities = await model.capabilities()
        check_supports(request, capabilities)
        input_estimate = estimate_input_tokens(request)
        max_output_tokens = request.settings.max_output_tokens
        assert max_output_tokens is not None  # ModelRequest requires every setting.
        check_fits_model(input_estimate, max_output_tokens, self.limits, capabilities)
        reservation = input_estimate + max_output_tokens
        self.accounting.reserve(reservation, self.limits)
        try:
            if on_text is not None and isinstance(model, StreamingModel):
                response = await model.stream(request, on_text)
            else:
                response = await model.generate(request)
        except UnsupportedCapabilityError:
            self.accounting.release(reservation)  # Raised before anything was sent.
            raise
        except ModelError as error:
            self.accounting.settle(reservation, error.usage or UNKNOWN_USAGE)
            self.accounting.count_hosted_tool_calls(
                error.hosted_tool_calls, self.limits
            )
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
    tools: Mapping[str, "ToolDefinition"], bound_tools: Mapping[str, BoundTool]
) -> None:
    """Refuse a remote or MCP tool named like a selected ``@tool``, before any
    model call.

    Raises:
        DefinitionError: A name is used by both.
    """
    for name, bound in bound_tools.items():
        if name in tools:
            raise DefinitionError(
                f"'{bound.entry.name}' and tool '{name}' both "
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
            offers tools or a hosted tool, and the model does not report
            support for it.
    """
    if request.output is not None and not capabilities.structured_output:
        raise UnsupportedCapabilityError("The model does not support structured output")
    if request.tools and not capabilities.tools:
        raise UnsupportedCapabilityError("The model does not support tool calling")
    for hosted in request.hosted_tools:
        if type(hosted) not in capabilities.hosted_tools:
            raise UnsupportedCapabilityError(
                f"The model does not support the hosted tool {type(hosted).__name__}"
            )


def check_history(history: Sequence[Message]) -> None:
    """Check that ``history`` holds only plain ``user`` and ``assistant`` messages.

    History is untrusted, so it can never add instructions as a ``system``
    message, or tool calls and results the run did not make.

    Raises:
        DefinitionError: A message has another role or carries tool calls.
    """
    for message in history:
        if message.role not in ("user", "assistant") or message.tool_calls:
            raise DefinitionError(
                "history may only hold user and assistant messages without tool calls"
            )


def build_messages(
    agent_instructions: str | None,
    call_instructions: str | None,
    text: str | Sequence[Message],
    history: Sequence[Message] = (),
) -> tuple[Message, ...]:
    """Put trusted instructions in ``system`` messages, then the untrusted ``text``.

    ``history``, earlier messages of the conversation, comes between the
    instructions and ``text``. ``text`` is one ``user`` message, or a
    conversation of messages sent as given. Untrusted text is never merged
    into instructions. This keeps the roles separate; it does not guarantee
    resistance to prompt injection.
    """
    messages = [
        Message(role="system", content=instructions)
        for instructions in (agent_instructions, call_instructions)
        if instructions
    ]
    messages.extend(history)
    if isinstance(text, str):
        messages.append(Message(role="user", content=text))
    else:
        messages.extend(text)
    return tuple(messages)
