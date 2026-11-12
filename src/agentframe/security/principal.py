"""Verified caller identity."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    """A verified caller identity. Created by trusted code, never from arguments.

    Attributes:
        id: Stable identifier of the caller.
        permissions: Application-defined permission strings granted to the caller.
    """

    id: str
    permissions: frozenset[str] = frozenset()
