"""The evaluation-wide token budget that runs hold their ceilings against."""

import asyncio


class TokenBudget:
    """Admits runs only while their token ceilings fit in the budget.

    Each run holds its full per-run token ceiling before it starts, so the
    sum of holds never exceeds the budget, however many runs are in flight.
    When a run finishes, its reported usage replaces its hold, even when it
    is higher (an overrun the report shows). Unknown usage keeps the whole
    hold charged, since the run may have used up to its ceiling.

    A run that does not fit waits while other runs are in flight, because
    they may use less than they hold. It is refused only when nothing else
    is running and it still does not fit.

    Args:
        limit: The budget in tokens, or ``None`` for no budget: every hold
            then succeeds at once.
    """

    def __init__(self, limit: int | None) -> None:
        self.limit = limit
        self.charged = 0
        self._running = 0
        self._changed = asyncio.Condition()

    async def hold(self, tokens: int) -> bool:
        """Hold ``tokens`` for one run, waiting while other runs may free some.

        Returns:
            ``True`` when the run may start; it must then call ``settle``.
            ``False`` when the budget cannot fit it.
        """
        async with self._changed:
            await self._changed.wait_for(
                lambda: self._fits(tokens) or self._running == 0
            )
            if not self._fits(tokens):
                return False
            self.charged += tokens
            self._running += 1
            return True

    async def settle(self, held: int, used: int | None) -> None:
        """Replace a finished run's hold with the tokens it reported using.

        Args:
            held: The tokens the run held.
            used: The tokens it reported, or ``None`` when unknown, which
                keeps the whole hold charged.
        """
        async with self._changed:
            if used is not None:
                self.charged += used - held
            self._running -= 1
            self._changed.notify_all()

    def _fits(self, tokens: int) -> bool:
        return self.limit is None or self.charged + tokens <= self.limit
