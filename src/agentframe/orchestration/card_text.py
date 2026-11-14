"""Bound the untrusted text other agents' cards put into a skill catalog.

Card descriptions, tags and argument schemas come from other parties. They
reach a model as data, so they are cut to a fixed size, stripped of control
characters and never copied with their ``examples``.
"""

import json
import unicodedata
from collections.abc import Iterable
from typing import Any

MAX_TEXT_CHARACTERS = 500
"""The longest description (or tag, or schema ``description``/``title``) kept."""

MAX_TAGS = 10
"""The most tags kept per catalog entry."""

MAX_SCHEMA_BYTES = 8192
"""The largest argument schema, as compact JSON after bounding, accepted."""

_CONTROL_CATEGORIES = {"Cc", "Cf"}
"""Unicode control and format characters, such as bidirectional overrides and
zero-width characters, which can hide text from a human reader."""

_TEXT_KEYWORDS = {"description", "title"}

_SCHEMA_MAP_KEYWORDS = {
    "properties",
    "patternProperties",
    "$defs",
    "definitions",
    "dependentSchemas",
}
"""Keywords whose value maps names to schemas; the names are not keywords."""

_SCHEMA_KEYWORDS = {
    "items",
    "prefixItems",
    "additionalItems",
    "additionalProperties",
    "unevaluatedItems",
    "unevaluatedProperties",
    "propertyNames",
    "contains",
    "not",
    "if",
    "then",
    "else",
    "allOf",
    "anyOf",
    "oneOf",
}
"""Keywords whose value is a schema or a list of schemas."""


def bounded_text(text: str) -> str:
    """Return ``text`` without control characters, cut to 500 characters.

    Whitespace control characters, such as newlines and tabs, become spaces
    so words stay apart; other control and format characters are removed.
    """
    kept: list[str] = []
    for character in text:
        if character.isspace():
            kept.append(" ")
        elif unicodedata.category(character) not in _CONTROL_CATEGORIES:
            kept.append(character)
    return "".join(kept)[:MAX_TEXT_CHARACTERS].strip()


def bounded_tags(tags: Iterable[str]) -> tuple[str, ...]:
    """Return the first ten distinct, non-empty tags, each bounded as text."""
    kept: list[str] = []
    for tag in tags:
        bounded = bounded_text(tag)
        if bounded and bounded not in kept:
            kept.append(bounded)
        if len(kept) == MAX_TAGS:
            break
    return tuple(kept)


def bounded_schema(schema: Any) -> Any:
    """Return a copy of a JSON Schema with its text bounded and no examples.

    ``description`` and ``title`` strings are bounded like card text and
    ``examples`` are dropped, in the schema and every subschema. Property
    names and every other keyword are kept unchanged.
    """
    if not isinstance(schema, dict):
        return schema  # true and false are schemas too.
    bounded: dict[str, Any] = {}
    for keyword, value in schema.items():
        if keyword == "examples":
            continue
        if keyword in _TEXT_KEYWORDS and isinstance(value, str):
            bounded[keyword] = bounded_text(value)
        elif keyword in _SCHEMA_MAP_KEYWORDS and isinstance(value, dict):
            bounded[keyword] = {name: bounded_schema(s) for name, s in value.items()}
        elif keyword in _SCHEMA_KEYWORDS and isinstance(value, list):
            bounded[keyword] = [bounded_schema(s) for s in value]
        elif keyword in _SCHEMA_KEYWORDS:
            bounded[keyword] = bounded_schema(value)
        else:
            bounded[keyword] = value
    return bounded


def schema_size(schema: Any) -> int:
    """Return the size of ``schema`` in bytes, as compact UTF-8 JSON."""
    compact = json.dumps(schema, separators=(",", ":"), ensure_ascii=False)
    return len(compact.encode())
