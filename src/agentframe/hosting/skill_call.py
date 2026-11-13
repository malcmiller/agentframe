"""The AgentFrame A2A extension: skill calls and the context callers pass along.

One extension URI covers both. A caller activates it with the
``A2A-Extensions`` header; its message is a skill call when its metadata names
a skill under ``agentframe.skill``, and free text otherwise. Either way the
metadata may carry context about the caller's own run:

- ``agentframe.deadline_ms``: the milliseconds the caller had left when it
  sent the message. Relative, so the two clocks need not agree.
- ``agentframe.hops``: the request's remote-call depth, the caller's own
  depth plus one. A request without it is a root request, depth 0.
- ``agentframe.max_total_tokens``: the most tokens the caller lets this
  request use. The receiver's run uses the smaller of it and its own
  ``RunLimits.max_total_tokens``; the task's usage snapshot reports what it
  actually used.
- ``agentframe.root_invocation_id`` and ``agentframe.parent_invocation_id``:
  the invocation IDs of the run that started the whole request and of the
  calling run, for logs only. A value that is not a lowercase UUID string is
  ignored, and an invalid or missing root makes the request a new root; they
  never fail a request, since they authenticate nothing.

A receiver lists the context fields it supports in its card's extension
``params["context"]``; a sender sends only the fields its peer lists. An agent
that lists none (such as AgentFrame 0.1.0) still takes skill calls, and free
text without the extension.

Kept apart from the card and executor modules so ``Remote`` can use it
without importing the hosting machinery.
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass

from a2a.types import AgentCard, Message
from a2a.utils.errors import InvalidParamsError

from agentframe.errors import LimitExceededError
from agentframe.runtime.correlation import is_invocation_id

SKILL_CALL_EXTENSION_URI = (
    "https://github.com/malcmiller/agentframe/extensions/skill-call/v1"
)
"""The extension a caller activates, per request, to call a skill by ID or
pass its context."""

METADATA_NAMESPACE = "agentframe."
"""Prefix of the message metadata keys the extension owns."""

SKILL_METADATA_KEY = "agentframe.skill"
"""Message metadata key that names the skill to run."""

CONTEXT_PARAM = "context"
"""Key of the context field list in the card's extension ``params``."""

DEADLINE_FIELD = "deadline_ms"
HOPS_FIELD = "hops"
MAX_TOTAL_TOKENS_FIELD = "max_total_tokens"
ROOT_INVOCATION_FIELD = "root_invocation_id"
PARENT_INVOCATION_FIELD = "parent_invocation_id"

CONTEXT_FIELDS = (
    DEADLINE_FIELD,
    HOPS_FIELD,
    MAX_TOTAL_TOKENS_FIELD,
    ROOT_INVOCATION_FIELD,
    PARENT_INVOCATION_FIELD,
)
"""The context fields this version reads and advertises on its card."""

KNOWN_METADATA_KEYS = frozenset(
    [SKILL_METADATA_KEY, *(METADATA_NAMESPACE + name for name in CONTEXT_FIELDS)]
)


@dataclass(frozen=True)
class CallerContext:
    """Context a calling agent sent along with its message.

    Attributes:
        deadline_seconds: The time the caller had left when it sent the
            message, or ``None`` when it sent none.
        hops: The request's remote-call depth; 0 for a root request.
        max_total_tokens: The most tokens the caller lets the request use,
            or ``None`` when it sent no ceiling.
        root_invocation_id: The run that started the whole request, or
            ``None`` for a root request.
        parent_invocation_id: The calling run, or ``None`` when the caller
            sent no valid root or parent.
    """

    deadline_seconds: float | None = None
    hops: int = 0
    max_total_tokens: int | None = None
    root_invocation_id: str | None = None
    parent_invocation_id: str | None = None


def read_caller_context(message: Message) -> CallerContext:
    """Read the context fields of a message that activated the extension.

    Args:
        message: The caller's message.

    Returns:
        The caller's context; fields it did not send, and invalid invocation
        IDs, keep their defaults.

    Raises:
        InvalidParamsError: The message carries an unknown ``agentframe.``
            metadata field, or a context field that is not a whole number in
            range (``deadline_ms`` and ``max_total_tokens`` at least 1,
            ``hops`` at least 0).
    """
    metadata = dict(message.metadata.items())
    unknown = sorted(
        key
        for key in metadata
        if key.startswith(METADATA_NAMESPACE) and key not in KNOWN_METADATA_KEYS
    )
    if unknown:
        raise InvalidParamsError(
            f"Unknown AgentFrame metadata fields: {', '.join(unknown)}."
        )
    deadline_ms = _whole_number(metadata, DEADLINE_FIELD, 1)
    root = _invocation_id(metadata, ROOT_INVOCATION_FIELD)
    # A parent means nothing without the root it belongs to.
    parent = _invocation_id(metadata, PARENT_INVOCATION_FIELD) if root else None
    return CallerContext(
        deadline_seconds=None if deadline_ms is None else deadline_ms / 1000,
        hops=_whole_number(metadata, HOPS_FIELD, 0) or 0,
        max_total_tokens=_whole_number(metadata, MAX_TOTAL_TOKENS_FIELD, 1),
        root_invocation_id=root,
        parent_invocation_id=parent,
    )


def _invocation_id(metadata: dict[str, object], field: str) -> str | None:
    """Return a context field's invocation ID, or ``None`` when it was not
    sent or is not one. Never an error: these only label logs."""
    value = metadata.get(METADATA_NAMESPACE + field)
    return value if is_invocation_id(value) else None


def _whole_number(metadata: dict[str, object], field: str, minimum: int) -> int | None:
    """Return a context field's value, or ``None`` when it was not sent."""
    key = METADATA_NAMESPACE + field
    if key not in metadata:
        return None
    value = metadata[key]
    # JSON numbers arrive as floats; bool is an int subclass but not a number here.
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not float(value).is_integer()
        or value < minimum
    ):
        raise InvalidParamsError(
            f"Metadata '{key}' must be a whole number of at least {minimum}."
        )
    return int(value)


def advertised_context(card: AgentCard) -> frozenset[str]:
    """Return the context fields a peer's card says it supports.

    Only fields this version knows are returned. A card without the
    extension, or whose extension lists none, supports none.
    """
    for extension in card.capabilities.extensions:
        if extension.uri != SKILL_CALL_EXTENSION_URI:
            continue
        if CONTEXT_PARAM not in extension.params:
            return frozenset()
        listed = extension.params[CONTEXT_PARAM]
        if not isinstance(listed, Sequence) or isinstance(listed, str):
            return frozenset()
        return frozenset(name for name in listed if name in CONTEXT_FIELDS)
    return frozenset()


def context_metadata(
    fields: Collection[str],
    *,
    remaining_seconds: float,
    hops: int,
    max_total_tokens: int | None = None,
    root_invocation_id: str | None = None,
    parent_invocation_id: str | None = None,
) -> dict[str, int | str]:
    """Return the message metadata that passes the caller's context on.

    Args:
        fields: The context fields the peer advertises; only these are sent.
        remaining_seconds: The time the caller's run has left.
        hops: The depth of the request being sent.
        max_total_tokens: The request's token ceiling, or ``None`` to send none.
        root_invocation_id: The run that started the whole request, or
            ``None`` to send none.
        parent_invocation_id: The calling run, or ``None`` to send none.
    """
    values: dict[str, int | str | None] = {
        # Rounded down, so the peer never gets more time than there is; at
        # least 1, the smallest valid value.
        DEADLINE_FIELD: max(1, int(remaining_seconds * 1000)),
        HOPS_FIELD: hops,
        MAX_TOTAL_TOKENS_FIELD: max_total_tokens,
        ROOT_INVOCATION_FIELD: root_invocation_id,
        PARENT_INVOCATION_FIELD: parent_invocation_id,
    }
    return {
        METADATA_NAMESPACE + name: value
        for name, value in values.items()
        if name in fields and value is not None
    }


def depth_exceeded(max_hops: int, invocation_id: str | None) -> LimitExceededError:
    """Return the error for a request deeper than ``max_hops`` remote calls."""
    return LimitExceededError(
        f"Maximum remote call depth of {max_hops} exceeded",
        invocation_id=invocation_id,
    )
