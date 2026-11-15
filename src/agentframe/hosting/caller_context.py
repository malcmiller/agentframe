"""Read the context a calling agent sent with its message.

The fields and their meaning are described in ``agentframe.skill_call``.
"""

from dataclasses import dataclass

from a2a.types import Message
from a2a.utils.errors import InvalidParamsError

from agentframe.runtime.correlation import is_invocation_id
from agentframe.skill_call import (
    CONTEXT_FIELDS,
    DEADLINE_FIELD,
    HOPS_FIELD,
    MAX_TOTAL_TOKENS_FIELD,
    METADATA_NAMESPACE,
    PARENT_INVOCATION_FIELD,
    ROOT_INVOCATION_FIELD,
    SKILL_METADATA_KEY,
)

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
