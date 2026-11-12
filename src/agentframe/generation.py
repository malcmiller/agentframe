"""Per-run model generation behind ``RunContext.generate``.

Includes token accounting and retries of transient provider failures.
"""

import asyncio
import math
import random
from dataclasses import dataclass, field

from agentframe.errors import (
    DefinitionError,
    IncompleteOutputError,
    ModelCallLimitExceededError,
    ModelError,
    ModelRefusalError,
    ProviderError,
    TimeoutLimitExceededError,
    TokenLimitExceededError,
    UnsupportedCapabilityError,
)
from agentframe.limits import RunLimits
from agentframe.models import (
    Message,
    Model,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    ModelSettings,
)
from agentframe.usage import FinishReason, Usage

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


@dataclass
class RunAccounting:
    """The tokens and model calls of one run, shared by all its model calls.

    Before a call, ``reserve`` holds the most tokens it can use (its input
    estimate plus its output cap). After it, ``settle`` replaces the hold with
    the tokens actually used, or keeps the hold when they are unknown, so a
    failed call is never treated as free. Neither method awaits, so
    concurrent calls in one run cannot both take the same remaining tokens.

    Attributes:
        usage: Tokens used by finished calls, as reported or estimated.
        model_calls: Calls started so far, including failed and running ones.
        committed_tokens: Tokens finished calls count against
            ``max_total_tokens``: their actual totals, or their reservations
            when the totals are unknown.
        reserved_tokens: Tokens held for calls that are still running.
    """

    usage: Usage = NO_USAGE
    model_calls: int = 0
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
    """One run's model, effective settings and accounting.

    The runtime creates a fresh state for every run, so concurrent runs never
    share usage totals or call counts.

    Attributes:
        model: The agent's model, or ``None`` when it has none.
        instructions: The agent's shared trusted instructions, if any.
        settings: Effective settings for every model call in this run.
        limits: Effective limits for this run.
        accounting: Tokens and model calls this run has used so far.
    """

    model: Model | None
    instructions: str | None
    settings: ModelSettings
    limits: RunLimits
    accounting: RunAccounting = field(default_factory=RunAccounting)

    async def generate_text(
        self, text: str, instructions: str | None, deadline: float
    ) -> str:
        """Send ``text`` to the model and return its complete answer.

        Args:
            text: Untrusted content, sent as a ``user`` message.
            instructions: Trusted call-specific instructions, sent after the
                agent's own instructions as a separate ``system`` message.
            deadline: Event-loop time when the run times out.

        Raises:
            UnsupportedCapabilityError: The agent has no model, or the model
                lacks something the request needs.
            DefinitionError: The model reports no context window and no
                ``max_input_tokens`` is set.
            TokenLimitExceededError: The call does not fit the input limit,
                the context window or the run's remaining tokens.
            ModelCallLimitExceededError: The run has made ``max_model_calls``.
            ModelRefusalError: The model refused, or a content filter blocked it.
            IncompleteOutputError: The output was cut short or empty.
            ProviderError: The provider call failed, and was not transient,
                used every retry, or would have to wait past the deadline.
            TimeoutLimitExceededError: The run's deadline passed during the call.
        """
        if self.model is None:
            raise UnsupportedCapabilityError("This agent has no model configured")
        messages = build_messages(self.instructions, instructions, text)
        request = ModelRequest(messages=messages, settings=self.settings)
        try:
            async with asyncio.timeout_at(deadline) as timeout:
                response = await self._call_with_retries(self.model, request, deadline)
        except TimeoutError:
            if not timeout.expired():
                raise
            raise TimeoutLimitExceededError(
                "The run's timeout expired during a model call"
            ) from None
        return text_from(response)

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
        input_estimate = estimate_input_tokens(request.messages)
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
            reservation, with_estimates(response.usage, input_estimate, response.text)
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


def estimate_input_tokens(messages: tuple[Message, ...]) -> int:
    """Estimate the input tokens of ``messages`` from their text."""
    return sum(estimate_tokens(message.content) for message in messages)


def with_estimates(reported: Usage, input_estimate: int, output_text: str) -> Usage:
    """Fill in token counts a provider did not report with estimates.

    Args:
        reported: The usage the model reported; unknown counts are ``None``.
        input_estimate: The estimated input tokens of the call.
        output_text: The text the model returned.

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
        output_tokens = estimate_tokens(output_text)
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        estimated=True,
    )


def build_messages(
    agent_instructions: str | None, call_instructions: str | None, text: str
) -> tuple[Message, ...]:
    """Put trusted instructions in ``system`` messages and ``text`` in a ``user`` one.

    Untrusted text is never merged into instructions. This keeps the roles
    separate; it does not guarantee resistance to prompt injection.
    """
    messages = [
        Message(role="system", content=instructions)
        for instructions in (agent_instructions, call_instructions)
        if instructions
    ]
    messages.append(Message(role="user", content=text))
    return tuple(messages)


def text_from(response: ModelResponse) -> str:
    """Return a response's text, or raise when it is not a complete answer.

    Raises:
        IncompleteOutputError: Output stopped at the token limit or is empty.
        ModelRefusalError: The model refused or a content filter blocked it.
    """
    match response.finish_reason:
        case FinishReason.LENGTH:
            raise IncompleteOutputError(
                "The model's output was cut short at its output token limit"
            )
        case FinishReason.REFUSED:
            raise ModelRefusalError("The model refused the request")
        case FinishReason.CONTENT_FILTER:
            raise ModelRefusalError(
                "The model's output was blocked by a content filter"
            )
    if not response.text:
        raise IncompleteOutputError("The model returned no text")
    return response.text


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
