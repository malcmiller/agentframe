"""Hold a remote request's token ceiling against its run, then settle it."""

from dataclasses import dataclass

from a2a.types import Message, Task

from agentframe.discovery.replies import reported_usage
from agentframe.errors import TokenLimitExceededError
from agentframe.runtime.accounting import UNKNOWN_USAGE, RunAccounting
from agentframe.skill_call import MAX_TOTAL_TOKENS_FIELD
from agentframe.usage import Usage


@dataclass
class Charge:
    """One remote request's hold on its run's token budget.

    Attributes:
        tokens: The tokens held, also sent as the request's ceiling. 0 for a
            peer that takes no ceiling.
        reports_usage: Whether the peer advertises ``max_total_tokens``, so
            the usage its task reports is charged to the run.
        sent: Whether the request may have reached the peer.
    """

    tokens: int
    reports_usage: bool
    sent: bool = False


class TokenHolds:
    """One ``Remote`` binding's holds on its run's token budget.

    Args:
        accounting: The run's accounting, which every request's tokens are
            held against and charged to.
        max_total_tokens: The run's ``RunLimits.max_total_tokens``.
        context_fields: The context fields the agent's card lists.
        agent: How errors name the agent.
        invocation_id: The run that owns the binding.
    """

    def __init__(
        self,
        accounting: RunAccounting,
        max_total_tokens: int,
        context_fields: frozenset[str],
        agent: str,
        invocation_id: str,
    ) -> None:
        self._accounting = accounting
        self._max_total_tokens = max_total_tokens
        self._context_fields = context_fields
        self._agent = agent
        self._invocation_id = invocation_id

    def hold(self, max_total_tokens: int | None) -> Charge:
        """Hold the request's ceiling against the run's tokens, without awaiting.

        Not awaiting means concurrent calls in one run cannot both take the
        same tokens. A peer that does not advertise ``max_total_tokens``
        takes no ceiling, so nothing is held.

        Raises:
            TokenLimitExceededError: The ceiling does not fit what is left.
        """
        if MAX_TOTAL_TOKENS_FIELD not in self._context_fields:
            return Charge(tokens=0, reports_usage=False)
        run_total = self._max_total_tokens
        remaining = self._accounting.remaining_tokens(run_total)
        if max_total_tokens is None and remaining == 0:
            raise TokenLimitExceededError(
                f"{self._agent}: none of the run's {run_total} tokens are left "
                "for the call. Without max_total_tokens a call holds all the "
                "run has left, so when calling agents concurrently, pass "
                "max_total_tokens to each call to share the budget",
                invocation_id=self._invocation_id,
            )
        if max_total_tokens is not None and max_total_tokens > remaining:
            raise TokenLimitExceededError(
                f"{self._agent}: the call needs up to {max_total_tokens} "
                f"tokens, but only {remaining} of the run's {run_total} remain",
                invocation_id=self._invocation_id,
            )
        tokens = remaining if max_total_tokens is None else max_total_tokens
        self._accounting.hold_tokens(tokens)
        return Charge(tokens=tokens, reports_usage=True)

    def settle(self, charge: Charge, reply: Task | Message | None) -> Usage | None:
        """Replace the request's hold with the usage its peer reported.

        A request that was never sent used nothing. Otherwise usage the peer
        does not report, or reports in a malformed way, is unknown: the hold
        stays counted and the run's usage becomes unknown, never zero.
        Reported usage above the hold is charged as reported.

        Args:
            charge: The request's hold.
            reply: The finished reply, or ``None`` when the request failed.

        Returns:
            The usage the peer reported, or ``None`` when it is unknown or
            the request was never sent.
        """
        if not charge.sent:
            self._accounting.release_tokens(charge.tokens)
            return None
        usage = None
        if charge.reports_usage and isinstance(reply, Task):
            usage = reported_usage(reply)
        self._accounting.settle(charge.tokens, usage or UNKNOWN_USAGE)
        return usage
