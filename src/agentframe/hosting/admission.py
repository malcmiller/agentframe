"""Admit at most a fixed number of hosted runs at once.

The app takes a ``RunSlot`` for each ``SendMessage`` before the A2A SDK sees
it and refuses the request when none is free; nothing ever waits for a slot.
The executor marks the slot as started when the run begins and releases it
when the run ends, however it ends. When the SDK refuses a message before
running it, the run never starts and the app releases the slot instead.

Everything runs on one event loop, so a plain counter needs no lock.
"""

RUN_SLOT_STATE_KEY = "agentframe.run_slot"
"""``ServerCallContext.state`` key holding the request's ``RunSlot``."""


class RunAdmission:
    """Counts running runs and refuses any beyond ``max_runs``.

    Args:
        max_runs: How many runs may be admitted at once.
    """

    def __init__(self, max_runs: int) -> None:
        self._max_runs = max_runs
        self._running = 0

    def try_admit(self) -> "RunSlot | None":
        """Take a slot for one run, or return ``None`` when all are in use."""
        if self._running >= self._max_runs:
            return None
        self._running += 1
        return RunSlot(self)

    def _release(self) -> None:
        self._running -= 1


class RunSlot:
    """One admitted run's place; releasing it more than once has no effect."""

    def __init__(self, admission: RunAdmission) -> None:
        self._admission = admission
        self.started = False
        self._released = False

    def start(self) -> None:
        """Hand the slot to the run, which now releases it when it ends.

        The executor calls this before it publishes the task, and the SDK
        responds to ``SendMessage``, even with ``returnImmediately``, only
        after that first task event. So the app sees ``started`` before it
        decides whether to release the slot itself. Should a run ever start
        after the app released its slot, the run goes ahead but its own
        release is a no-op, so the count never goes negative.
        """
        self.started = True

    def release(self) -> None:
        """Free the slot for another run."""
        if self._released:
            return
        self._released = True
        # noinspection PyProtectedMember
        self._admission._release()
