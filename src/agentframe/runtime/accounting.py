"""One run's token, model-call and tool-call accounting."""

from dataclasses import dataclass

from agentframe.errors import (
    ModelCallLimitExceededError,
    TokenLimitExceededError,
    ToolCallLimitExceededError,
)
from agentframe.runtime.limits import RunLimits
from agentframe.usage import Usage

NO_USAGE = Usage(input_tokens=0, output_tokens=0, total_tokens=0, estimated=False)
UNKNOWN_USAGE = Usage(
    input_tokens=None, output_tokens=None, total_tokens=None, estimated=False
)


@dataclass
class RunAccounting:
    """The tokens, model calls and tool calls of one run.

    Before a call, ``reserve`` holds the most tokens it can use (its input
    estimate plus its output cap). After it, ``settle`` replaces the hold with
    the tokens actually used, or keeps the hold when they are unknown, so a
    failed call is never treated as free. Neither method awaits, so
    concurrent calls in one run cannot both take the same remaining tokens.
    Remote agent calls hold tokens the same way with ``hold_tokens``, without
    counting a model call, and settle with the usage the agent reports.

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
        remaining = self.remaining_tokens(max_total)
        if tokens > remaining:
            raise TokenLimitExceededError(
                f"The model call needs up to {tokens} tokens, but only "
                f"{remaining} of the run's {max_total} remain"
            )
        self.model_calls += 1
        self.reserved_tokens += tokens

    def release(self, tokens: int) -> None:
        """Undo a reservation for a call that was never sent."""
        self.model_calls -= 1
        self.reserved_tokens -= tokens

    def remaining_tokens(self, max_total: int) -> int:
        """Return the tokens of ``max_total`` that nothing holds or has used."""
        return max(max_total - self.committed_tokens - self.reserved_tokens, 0)

    def hold_tokens(self, tokens: int) -> None:
        """Hold ``tokens`` for a remote call; the caller checked that they fit."""
        self.reserved_tokens += tokens

    def release_tokens(self, tokens: int) -> None:
        """Undo ``hold_tokens`` for a remote call that was never sent."""
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
