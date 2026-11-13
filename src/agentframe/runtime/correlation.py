"""Correlation between the runs of one request that spans several agents.

A run started by a person or program is a root run. Each run it starts on
another agent, and each run those start, shares its invocation ID as their
``root_invocation_id``, so their logs can be followed together without a
tracing backend. A W3C ``traceparent`` (and ``tracestate``) that arrived with
the request goes unchanged to every agent the run calls; AgentFrame never
creates trace IDs or spans itself.

These values only label logs and travel on to peers. They come from the
caller, so they never authenticate anyone or grant anything, and malformed
ones are dropped rather than trusted or repeated.
"""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypeIs

TRACEPARENT_HEADER = "traceparent"
TRACESTATE_HEADER = "tracestate"

# Version 00 only: AgentFrame cannot check a later version's fields, so it
# forwards none rather than pass on something it has not validated.
_TRACEPARENT = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}")

# W3C trace context list members: a key, "=", and a value of printable ASCII
# without "," or "=" that does not end in a space.
_KEY_CHARS = r"[a-z0-9_\-*/]"
_SIMPLE_KEY = rf"[a-z]{_KEY_CHARS}{{0,255}}"
_TENANT_KEY = rf"[a-z0-9]{_KEY_CHARS}{{0,240}}@[a-z]{_KEY_CHARS}{{0,13}}"
_KEY = rf"{_TENANT_KEY}|{_SIMPLE_KEY}"
_VALUE = r"[\x20-\x2b\x2d-\x3c\x3e-\x7e]{0,255}[\x21-\x2b\x2d-\x3c\x3e-\x7e]"
_TRACESTATE_MEMBER = re.compile(rf"(?:{_KEY})=(?:{_VALUE})")
_MAX_TRACESTATE_LENGTH = 512
_MAX_TRACESTATE_MEMBERS = 32


@dataclass(frozen=True)
class TraceContext:
    """A validated W3C trace context, forwarded exactly as it arrived.

    Attributes:
        traceparent: The ``traceparent`` header value.
        tracestate: The ``tracestate`` header value, or ``None`` when the
            request had none or an invalid one.
    """

    traceparent: str
    tracestate: str | None = None

    def headers(self) -> dict[str, str]:
        """Return the HTTP headers that pass this trace context on."""
        headers = {TRACEPARENT_HEADER: self.traceparent}
        if self.tracestate is not None:
            headers[TRACESTATE_HEADER] = self.tracestate
        return headers


def read_trace_context(
    traceparents: Sequence[str], tracestates: Sequence[str]
) -> TraceContext | None:
    """Return a request's trace context, or ``None`` when it has no valid one.

    Args:
        traceparents: Every ``traceparent`` header of the request.
        tracestates: Every ``tracestate`` header of the request; several are
            joined with commas, as HTTP allows.

    Returns:
        The trace context when there is exactly one valid version-00
        ``traceparent``; its ``tracestate`` is dropped alone when invalid.
    """
    if len(traceparents) != 1 or not _is_traceparent(traceparents[0]):
        return None
    tracestate = ",".join(tracestates) if tracestates else None
    if tracestate is not None and not _is_tracestate(tracestate):
        tracestate = None
    return TraceContext(traceparents[0], tracestate)


def _is_traceparent(value: str) -> bool:
    match = _TRACEPARENT.fullmatch(value)
    # All-zero trace and parent IDs are invalid.
    return (
        match is not None and match.group(1) != "0" * 32 and match.group(2) != "0" * 16
    )


def _is_tracestate(value: str) -> bool:
    if len(value) > _MAX_TRACESTATE_LENGTH:
        return False
    members = [member.strip(" \t") for member in value.split(",")]
    members = [member for member in members if member]
    return len(members) <= _MAX_TRACESTATE_MEMBERS and all(
        _TRACESTATE_MEMBER.fullmatch(member) for member in members
    )


def is_invocation_id(value: object) -> TypeIs[str]:
    """Return whether ``value`` is an invocation ID: a lowercase UUID string."""
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


@dataclass(frozen=True)
class RunCorrelation:
    """Where one run sits in a request that may span several agents.

    Attributes:
        agent: The name of the agent the run belongs to.
        invocation_id: The run's own invocation ID.
        root_invocation_id: The invocation ID of the run that started the
            whole request; the run's own ID when it is that run.
        parent_invocation_id: The invocation ID of the run on another agent
            that called this one, or ``None`` for a root run.
        trace: The W3C trace context to forward, or ``None``.
    """

    agent: str
    invocation_id: str
    root_invocation_id: str
    parent_invocation_id: str | None = None
    trace: TraceContext | None = None

    def log_extra(self) -> dict[str, str | None]:
        """Return the ``extra`` for a log record about this run.

        AgentFrame sets no log format; a handler's formatter can show these
        as, for example, ``%(root_invocation_id)s``.
        """
        return {
            "agent": self.agent,
            "invocation_id": self.invocation_id,
            "root_invocation_id": self.root_invocation_id,
            "parent_invocation_id": self.parent_invocation_id,
        }


def root_run(agent: str, invocation_id: str) -> RunCorrelation:
    """Return the correlation of a run that no other agent started."""
    return RunCorrelation(agent, invocation_id, root_invocation_id=invocation_id)
