"""Per-run model generation behind ``RunContext.generate``.

Includes token accounting and retries of transient provider failures.
"""

import asyncio
import json
import math
import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, RootModel, ValidationError

from agentframe.authoring.services import ServiceScope
from agentframe.errors import (
    DefinitionError,
    IncompleteOutputError,
    ModelCallLimitExceededError,
    ModelError,
    ModelRefusalError,
    OutputValidationError,
    ProviderError,
    TimeoutLimitExceededError,
    TokenLimitExceededError,
    ToolCallLimitExceededError,
    UnsupportedCapabilityError,
    describe_validation_error,
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
from agentframe.runtime.limits import RunLimits
from agentframe.runtime.tools import (
    assistant_message,
    is_permitted,
    run_tool_call,
    tool_message,
    tool_spec,
)
from agentframe.runtime.usage import FinishReason, Usage
from agentframe.security.execution_policy import ExecutionPolicy, enforce_checks

if TYPE_CHECKING:
    # Imported only for type checking: these modules import this one.
    from agentframe.authoring.context import RunContext
    from agentframe.authoring.definitions import SkillDefinition, ToolDefinition
    from agentframe.security.authorization import ToolAuthorizationPolicy

NO_USAGE = Usage(input_tokens=0, output_tokens=0, total_tokens=0, estimated=False)
UNKNOWN_USAGE = Usage(
    input_tokens=None, output_tokens=None, total_tokens=None, estimated=False
)

CHARACTERS_PER_TOKEN = 4
"""Characters counted as one token when a provider reports no token count.

A common rule of thumb for English text. It is a best-effort estimate, not a
guaranteed bound: some text, such as code or other languages, uses more tokens.
"""

INITIAL_BACKOFF_SECONDS = 0.5
"""Longest wait before the first retry when the provider gives no hint."""

MAX_BACKOFF_SECONDS = 8.0
"""Longest wait before any retry when the provider gives no hint."""

TRUNCATED_MESSAGE = "The model's output was cut short at its output token limit"


@dataclass
class RunAccounting:
    """The tokens, model calls and tool calls of one run.

    Before a call, ``reserve`` holds the most tokens it can use (its input
    estimate plus its output cap). After it, ``settle`` replaces the hold with
    the tokens actually used, or keeps the hold when they are unknown, so a
    failed call is never treated as free. Neither method awaits, so
    concurrent calls in one run cannot both take the same remaining tokens.

    Attributes:
        usage: Tokens used by finished calls, as reported or estimated.
        model_calls: Calls started so far, including failed and running ones.
        tool_calls: Tool calls the models asked for so far, including ones
            that did not run because of an unknown tool or invalid arguments.
        committed_tokens: Tokens finished calls count against
            ``max_total_tokens``: their actual totals, or their reservations
            when the totals are unknown.
        reserved_tokens: Tokens held for calls that are still running.
    """

    usage: Usage = NO_USAGE
    model_calls: int = 0
    tool_calls: int = 0
    committed_tokens: int = 0
    reserved_tokens: int = 0

    def current_usage(self) -> Usage:
        """Return the usage so far; counts are unknown while a call is running.

        Every reservation is positive, so held tokens mean a call is running.
        """
        if self.reserved_tokens:
            return add_usage(self.usage, UNKNOWN_USAGE)
        return self.usage

    def reserve(self, tokens: int, limits: RunLimits) -> None:
        """Count one more model call and hold ``tokens`` of the token budget.

        Raises:
            ModelCallLimitExceededError: The run has made ``max_model_calls``.
            TokenLimitExceededError: ``tokens`` do not fit what is left of
                ``max_total_tokens``.
        """
        max_calls, max_total = limits.max_model_calls, limits.max_total_tokens
        assert max_calls is not None and max_total is not None  # Set by defaults.
        if self.model_calls >= max_calls:
            raise ModelCallLimitExceededError(
                f"The run reached its limit of {max_calls} model calls"
            )
        remaining = max_total - self.committed_tokens - self.reserved_tokens
        if tokens > remaining:
            raise TokenLimitExceededError(
                f"The model call needs up to {tokens} tokens, but only "
                f"{max(remaining, 0)} of the run's {max_total} remain"
            )
        self.model_calls += 1
        self.reserved_tokens += tokens

    def release(self, tokens: int) -> None:
        """Undo a reservation for a call that was never sent."""
        self.model_calls -= 1
        self.reserved_tokens -= tokens

    def count_tool_call(self, limits: RunLimits) -> None:
        """Count one more tool call, before it runs.

        Raises:
            ToolCallLimitExceededError: The run has made ``max_tool_calls``.
        """
        max_calls = limits.max_tool_calls
        assert max_calls is not None  # Set by defaults.
        if self.tool_calls >= max_calls:
            raise ToolCallLimitExceededError(
                f"The run reached its limit of {max_calls} tool calls"
            )
        self.tool_calls += 1

    def settle(self, tokens: int, usage: Usage) -> None:
        """Replace a call's reservation of ``tokens`` with its actual ``usage``."""
        self.reserved_tokens -= tokens
        if usage.total_tokens is None:
            self.committed_tokens += tokens
        else:
            self.committed_tokens += usage.total_tokens
        self.usage = add_usage(self.usage, usage)


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
            model request, or ``None`` for no checks.
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
    ) -> str:
        """Send ``text`` to the model and return its complete answer.

        When ``tools`` are given, the model's tool calls are run and their
        results sent back until the model answers (see ``_generate``).

        Args:
            ctx: The run's context: its deadline, and what tools receive.
            text: Untrusted content, sent as a ``user`` message, or untrusted
                ``user`` and ``assistant`` messages sent in order.
            instructions: Trusted call-specific instructions, sent after the
                agent's own instructions as a separate ``system`` message.
            tools: Registered ``@tool`` functions to offer the model.

        Raises:
            DefinitionError: A selected tool is not registered on the agent
                or is selected twice, or the model reports no context window
                and no ``max_input_tokens`` is set.
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
        response = await self._generate(ctx, text, instructions, None, tools)
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

        Raises:
            DefinitionError: ``output`` is not a ``BaseModel`` subclass, or is
                a ``RootModel``.
            UnsupportedCapabilityError: The agent has no model, or the model
                does not support structured output.
            OutputValidationError: The reply is not valid JSON or does not
                match ``output``. The message lists field locations only.
        """
        schema = output_schema(output)
        response = await self._generate(ctx, text, instructions, schema, tools)
        return validate_output(text_from(response), output)

    async def _generate(
        self,
        ctx: "RunContext",
        text: str | Sequence[Message],
        instructions: str | None,
        output: OutputSchema | None,
        tools: Iterable[Callable[..., Any]],
    ) -> ModelResponse:
        """Run the model and the tools it calls within the run's deadline.

        Returns:
            The model's final response: the first one that calls no tools.
        """
        selected_tools = self.select_tools(tools)
        if self.model is None:
            raise UnsupportedCapabilityError("This agent has no model configured")
        messages = build_messages(self.instructions, instructions, text)
        try:
            async with asyncio.timeout_at(ctx.deadline) as timeout:
                return await self._tool_loop(
                    self.model, ctx, messages, output, selected_tools
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
    ) -> ModelResponse:
        """Call the model, run the tools it asks for, and repeat until it answers.

        Each model call is an ordinary call: it counts toward
        ``max_model_calls`` and the token budget. Tool calls in one response
        run one at a time, in the model's order, and each counts toward
        ``max_tool_calls`` before it runs. The loop ends because those limits
        are finite. A tool call is never retried.

        Tools the caller lacks a required permission for are left out of the
        request, and every call is authorized again before it runs. Every
        request, including each continuation, passes the execution policy's
        model-request checks before it is sent.
        """
        specs = tuple(
            tool_spec(tool)
            for tool in tools.values()
            if is_permitted(tool, ctx.principal)
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
                result = await run_tool_call(
                    call, tools, self.services, ctx, self.tool_authorization
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


def input_token_limit(limits: RunLimits, capabilities: ModelCapabilities) -> int:
    """Return the most input tokens one call may send.

    That is ``max_input_tokens`` when set, else the model's context window.

    Raises:
        DefinitionError: Neither is known. A context size is never guessed.
    """
    if limits.max_input_tokens is not None:
        return limits.max_input_tokens
    if capabilities.context_window is not None:
        return capabilities.context_window
    raise DefinitionError(
        "The model does not report its context window; set "
        "RunLimits(max_input_tokens=...) or give the model a context window"
    )


def check_fits_model(
    input_tokens: int,
    max_output_tokens: int,
    limits: RunLimits,
    capabilities: ModelCapabilities,
) -> None:
    """Check that a call's input, plus its output cap, fit the model's limits.

    Oversized input fails; it is never truncated, and the output cap is never
    lowered to make room.

    Raises:
        DefinitionError: No input limit is known (see ``input_token_limit``).
        TokenLimitExceededError: The input exceeds the input limit, or input
            plus output exceeds the context window.
    """
    max_input = input_token_limit(limits, capabilities)
    if input_tokens > max_input:
        raise TokenLimitExceededError(
            f"The model input is about {input_tokens} tokens, over the "
            f"limit of {max_input} input tokens"
        )
    context_window = capabilities.context_window
    if context_window is not None and input_tokens + max_output_tokens > context_window:
        raise TokenLimitExceededError(
            f"The model input of about {input_tokens} tokens plus up to "
            f"{max_output_tokens} output tokens exceeds the model's context "
            f"window of {context_window} tokens"
        )


def estimate_tokens(text: str) -> int:
    """Estimate the tokens in ``text`` from its length (``CHARACTERS_PER_TOKEN``)."""
    return math.ceil(len(text) / CHARACTERS_PER_TOKEN)


def estimate_input_tokens(request: ModelRequest) -> int:
    """Estimate the input tokens of ``request``.

    Counts every message, including tool calls and tool results, plus the
    output schema and the tool specs.
    """
    tokens = sum(estimate_message_tokens(message) for message in request.messages)
    if request.output is not None:
        tokens += estimate_tokens(json.dumps(request.output.json_schema))
    for spec in request.tools:
        tokens += estimate_tokens(spec.name + spec.description)
        tokens += estimate_tokens(json.dumps(spec.parameters))
    return tokens


def estimate_message_tokens(message: Message) -> int:
    """Estimate the tokens of one message: its text, tool name and tool calls."""
    tokens = estimate_tokens(message.content + (message.tool_name or ""))
    for call in message.tool_calls:
        tokens += estimate_tokens(call.name + call.arguments)
    return tokens


def estimate_output_tokens(response: ModelResponse) -> int:
    """Estimate the output tokens of ``response``: its text and tool calls."""
    tokens = estimate_tokens(response.text)
    for call in response.tool_calls:
        tokens += estimate_tokens(call.name + call.arguments)
    return tokens


def with_estimates(reported: Usage, input_estimate: int, output_estimate: int) -> Usage:
    """Fill in token counts a provider did not report with estimates.

    Args:
        reported: The usage the model reported; unknown counts are ``None``.
        input_estimate: The estimated input tokens of the call.
        output_estimate: The estimated output tokens of the response.

    Returns:
        Complete usage. ``estimated`` is ``True`` if any count was estimated.
    """
    input_tokens, output_tokens = reported.input_tokens, reported.output_tokens
    if input_tokens is not None and output_tokens is not None:
        if reported.total_tokens is not None:
            return reported
        return Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            estimated=reported.estimated,
        )
    if input_tokens is None:
        input_tokens = input_estimate
    if output_tokens is None:
        output_tokens = output_estimate
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        estimated=True,
    )


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


def text_from(response: ModelResponse) -> str:
    """Return a response's text, or raise when it is not a complete answer.

    Raises:
        IncompleteOutputError: Output stopped at the token limit or is empty.
        ModelRefusalError: The model refused or a content filter blocked it.
    """
    match response.finish_reason:
        case FinishReason.LENGTH:
            raise IncompleteOutputError(TRUNCATED_MESSAGE)
        case FinishReason.REFUSED:
            raise ModelRefusalError("The model refused the request")
        case FinishReason.CONTENT_FILTER:
            raise ModelRefusalError(
                "The model's output was blocked by a content filter"
            )
    if not response.text:
        raise IncompleteOutputError("The model returned no text")
    return response.text


def output_schema(output: type[BaseModel]) -> OutputSchema:
    """Return the schema the model must follow to produce an ``output`` instance.

    Raises:
        DefinitionError: ``output`` is not a ``BaseModel`` subclass, or is a
            ``RootModel``. Wrap lists and other values in a model field.
    """
    is_model = isinstance(output, type) and issubclass(output, BaseModel)
    if not is_model or issubclass(output, RootModel):
        raise DefinitionError(
            "generate(output=...) must be a Pydantic BaseModel subclass (not a "
            "RootModel); wrap lists and other values in a model field"
        )
    return OutputSchema(name=output.__name__, json_schema=output.model_json_schema())


def validate_output[T: BaseModel](text: str, output: type[T]) -> T:
    """Parse and validate the model's JSON ``text`` as an ``output`` instance.

    Raises:
        OutputValidationError: ``text`` is not valid JSON or does not match
            ``output``. The message names field locations, never the text;
            location parts that are not schema property names are masked.
    """
    try:
        return output.model_validate_json(text)
    except ValidationError as error:
        field_names = schema_property_names(output.model_json_schema())
        raise OutputValidationError(
            f"The model's output is not a valid '{output.__name__}': "
            f"{describe_validation_error(error, field_names)}"
        ) from None


def schema_property_names(schema: object) -> set[str]:
    """Return every property name declared anywhere in a JSON ``schema``.

    Error locations show only these names; other parts, such as dictionary
    keys the model chose, are masked.
    """
    names: set[str] = set()
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            names.update(properties)
        for value in schema.values():
            names |= schema_property_names(value)
    elif isinstance(schema, list):
        for item in schema:
            names |= schema_property_names(item)
    return names


def add_usage(total: Usage, extra: Usage) -> Usage:
    """Add two usages. A count unknown on either side stays unknown."""
    return Usage(
        input_tokens=_add(total.input_tokens, extra.input_tokens),
        output_tokens=_add(total.output_tokens, extra.output_tokens),
        total_tokens=_add(total.total_tokens, extra.total_tokens),
        estimated=total.estimated or extra.estimated,
    )


def _add(count: int | None, extra: int | None) -> int | None:
    if count is None or extra is None:
        return None
    return count + extra
