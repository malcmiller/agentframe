"""Convert Pydantic JSON schemas to the OpenAI strict structured-output subset.

Strict mode constrains a model's reply to a schema, but accepts only a subset
of JSON Schema: every object must list all of its properties as ``required``
and set ``"additionalProperties": false``, and ``$ref`` may not have sibling
keywords. ``strict_schema`` rewrites a Pydantic schema to fit, and rejects
constructs the subset cannot express before anything is sent.

Making every property required would erase Pydantic defaults, so each
property that was optional (has a default) becomes nullable instead. The
model answers ``null`` to mean "use the default", and
``drop_defaulted_nulls`` removes those nulls from the reply so Pydantic
applies the defaults. A property that was already nullable keeps its
``null``, because there ``null`` is a real value.

Used for ``text.format`` schemas and strict function parameter schemas.
See the subset at
https://developers.openai.com/api/docs/guides/structured-outputs#supported-schemas
"""

from typing import Any

from agentframe.errors import UnsupportedCapabilityError

UNSUPPORTED_KEYWORDS = (
    "allOf",
    "not",
    "if",
    "then",
    "else",
    "dependentRequired",
    "dependentSchemas",
    "oneOf",
    "patternProperties",
    "prefixItems",
)
"""Keywords strict mode does not support; a schema using any is rejected."""

TYPE_KEYWORDS = ("type", "anyOf", "$ref", "enum", "const")
"""Keywords that say what a value is; a schema without any allows anything."""

NULL_SCHEMA: dict[str, Any] = {"type": "null"}


def strict_schema(json_schema: dict[str, Any]) -> dict[str, Any]:
    """Return a strict-mode copy of a Pydantic model's JSON schema.

    The input is not modified. Every object gets ``"additionalProperties":
    false`` and all of its properties in ``required``; optional properties
    become nullable; ``default`` keywords are removed, since a required
    property never uses them; and a ``$ref`` with sibling keywords is wrapped
    in a one-branch ``anyOf``. Other supported keywords, such as
    ``description``, ``enum``, ``pattern`` and ``minimum``, are kept as-is.

    Args:
        json_schema: The schema from ``BaseModel.model_json_schema()``.

    Returns:
        The strict schema, with an object at its root.

    Raises:
        UnsupportedCapabilityError: The schema uses a construct strict mode
            cannot express: a non-object root, a free-form object such as
            ``dict[str, int]``, a tuple, a value of any type, or one of
            ``UNSUPPORTED_KEYWORDS``. The message names the construct and
            where it is, never a value.
    """
    definitions = json_schema.get("$defs", {})
    root = _resolve(json_schema, json_schema)
    if root.get("type") != "object":
        raise _unsupported("a root that is not an object", "")
    strict = _strict(root, "")
    if definitions:
        strict["$defs"] = {
            name: _strict(definition, name) for name, definition in definitions.items()
        }
    return strict


def drop_defaulted_nulls(value: Any, json_schema: dict[str, Any]) -> Any:
    """Remove the nulls that stand for defaults from a strict-mode reply.

    A ``null`` is removed only for a property that was optional and not
    nullable in ``json_schema``, so validation applies its default. Where an
    object could match several ``anyOf`` branches, it is left unchanged.

    Args:
        value: The parsed JSON reply.
        json_schema: The original Pydantic schema, not the strict one.

    Returns:
        A copy of ``value`` without those nulls.
    """
    return _drop_nulls(value, json_schema, json_schema)


def _strict(schema: dict[str, Any], location: str) -> dict[str, Any]:
    """Return a strict copy of one (sub)schema; ``$defs`` are handled by the caller.

    ``location`` is the dotted field path used in error messages, such as
    ``"points[]"`` or ``"Inner.a"``; ``""`` is the root.
    """
    for keyword in UNSUPPORTED_KEYWORDS:
        if keyword in schema:
            raise _unsupported(f"'{keyword}'", location)
    if not any(keyword in schema for keyword in TYPE_KEYWORDS):
        raise _unsupported("a value of any type", location)
    strict = {
        key: value
        for key, value in schema.items()
        if key not in ("default", "$defs", "$ref")
    }
    if "$ref" in schema:
        reference = {"$ref": schema["$ref"]}
        return {"anyOf": [reference], **strict} if strict else reference
    if "anyOf" in schema:
        strict["anyOf"] = [_strict(branch, location) for branch in schema["anyOf"]]
    if "items" in schema:
        strict["items"] = _strict(schema["items"], f"{location}[]")
    if schema.get("type") == "object":
        strict.update(_strict_object(schema, location))
    return strict


def _strict_object(schema: dict[str, Any], location: str) -> dict[str, Any]:
    """Return the strict ``properties``, ``required`` and ``additionalProperties``."""
    if "properties" not in schema or schema.get("additionalProperties", False):
        raise _unsupported("a free-form object (such as a dict)", location)
    required = set(schema.get("required", ()))
    properties = {}
    for name, property_schema in schema["properties"].items():
        strict = _strict(property_schema, f"{location}.{name}" if location else name)
        if name not in required and not _allows_null(property_schema):
            strict = _nullable(strict)
        properties[name] = strict
    return {
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    """Return ``schema`` that also accepts ``null``."""
    if "anyOf" in schema:
        return {**schema, "anyOf": [*schema["anyOf"], NULL_SCHEMA]}
    return {"anyOf": [schema, NULL_SCHEMA]}


def _allows_null(schema: dict[str, Any]) -> bool:
    kind = schema.get("type")
    if kind == "null" or (isinstance(kind, list) and "null" in kind):
        return True
    return any(_allows_null(branch) for branch in schema.get("anyOf", ()))


def _resolve(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    """Follow ``$ref`` to ``#`` or ``#/$defs/<name>``, the forms Pydantic uses."""
    while "$ref" in schema:
        reference = schema["$ref"]
        if reference == "#":
            schema = root
        elif reference.startswith("#/$defs/"):
            schema = root["$defs"][reference.removeprefix("#/$defs/")]
        else:
            return schema
    return schema


def _drop_nulls(value: Any, schema: dict[str, Any], root: dict[str, Any]) -> Any:
    branches = _branches(schema, root)
    if isinstance(value, list):
        item_schemas = [branch["items"] for branch in branches if "items" in branch]
        if len(item_schemas) != 1:
            return value
        return [_drop_nulls(item, item_schemas[0], root) for item in value]
    if isinstance(value, dict):
        matches = [
            branch
            for branch in branches
            if "properties" in branch and set(branch["properties"]) == set(value)
        ]
        if len(matches) != 1:
            return value
        return _drop_object_nulls(value, matches[0], root)
    return value


def _drop_object_nulls(
    value: dict[str, Any], schema: dict[str, Any], root: dict[str, Any]
) -> dict[str, Any]:
    required = set(schema.get("required", ()))
    result = {}
    for name, item in value.items():
        property_schema = schema["properties"][name]
        if item is None and name not in required and not _allows_null(property_schema):
            continue
        result[name] = _drop_nulls(item, property_schema, root)
    return result


def _branches(schema: dict[str, Any], root: dict[str, Any]) -> list[dict[str, Any]]:
    """Return ``schema`` and its ``anyOf`` branches, with references resolved."""
    schema = _resolve(schema, root)
    return [schema, *(_resolve(branch, root) for branch in schema.get("anyOf", ()))]


def _unsupported(construct: str, location: str) -> UnsupportedCapabilityError:
    return UnsupportedCapabilityError(
        f"Strict schemas do not support {construct} (at {location or 'the root'})"
    )
