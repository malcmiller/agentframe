"""Tests for ``strict_schema`` and ``drop_defaulted_nulls``.

Each test starts from a Pydantic model, as the Foundry adapter does.
"""

from enum import Enum
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

from agentframe import UnsupportedCapabilityError
from agentframe.models.strict_schema import drop_defaulted_nulls, strict_schema

NULL = {"type": "null"}


class Point(BaseModel):
    x: int
    y: int


class Shape(BaseModel):
    name: str = Field(description="Display name")
    points: list[Point]


class Node(BaseModel):
    name: str
    children: list["Node"] = []


class Defaults(BaseModel):
    points: list[str] = []
    n: int = 3
    x: int | None = 5


def strict(model: type[BaseModel]) -> dict[str, Any]:
    return strict_schema(model.model_json_schema())


def from_reply(model: type[BaseModel], reply: dict[str, Any]) -> BaseModel:
    """Validate a strict-mode reply the way the Foundry adapter returns it."""
    return model.model_validate(drop_defaulted_nulls(reply, model.model_json_schema()))


# Conversion


def test_every_object_is_closed_and_requires_all_properties() -> None:
    assert strict(Point) == {
        "type": "object",
        "title": "Point",
        "properties": {
            "x": {"type": "integer", "title": "X"},
            "y": {"type": "integer", "title": "Y"},
        },
        "required": ["x", "y"],
        "additionalProperties": False,
    }


def test_nested_models_and_lists_of_models_are_strict_definitions() -> None:
    schema = strict(Shape)

    assert schema["properties"]["points"]["items"] == {"$ref": "#/$defs/Point"}
    assert schema["$defs"]["Point"]["required"] == ["x", "y"]
    assert schema["$defs"]["Point"]["additionalProperties"] is False


def test_a_reference_with_sibling_keywords_is_wrapped_in_any_of() -> None:
    class Drawing(BaseModel):
        shape: Point = Field(description="The shape")

    assert strict(Drawing)["properties"]["shape"] == {
        "anyOf": [{"$ref": "#/$defs/Point"}],
        "description": "The shape",
    }


def test_defaulted_fields_become_nullable_and_required() -> None:
    schema = strict(Defaults)

    assert schema["required"] == ["points", "n", "x"]
    assert schema["properties"]["points"] == {
        "anyOf": [
            {"type": "array", "items": {"type": "string"}, "title": "Points"},
            NULL,
        ]
    }
    assert schema["properties"]["n"] == {
        "anyOf": [{"type": "integer", "title": "N"}, NULL]
    }


def test_an_already_nullable_field_gets_no_second_null_branch() -> None:
    assert strict(Defaults)["properties"]["x"] == {
        "anyOf": [{"type": "integer"}, NULL],
        "title": "X",
    }


def test_a_defaulted_model_reference_becomes_nullable_without_its_default() -> None:
    class Drawing(BaseModel):
        origin: Point = Point(x=0, y=0)

    assert strict(Drawing)["properties"]["origin"] == {
        "anyOf": [{"$ref": "#/$defs/Point"}, NULL]
    }


def test_enums_and_literals_are_kept() -> None:
    class Color(Enum):
        RED = "red"

    class Choice(BaseModel):
        color: Color
        size: Literal["s", "m"]
        kind: Literal["shape"]

    schema = strict(Choice)

    assert schema["$defs"]["Color"] == {
        "enum": ["red"],
        "title": "Color",
        "type": "string",
    }
    assert schema["properties"]["size"]["enum"] == ["s", "m"]
    assert schema["properties"]["kind"]["const"] == "shape"


def test_recursive_models_keep_their_references() -> None:
    schema = strict(Node)

    assert schema["type"] == "object"
    assert schema["required"] == ["name", "children"]
    assert schema["properties"]["children"]["anyOf"][0]["items"] == {
        "$ref": "#/$defs/Node"
    }
    assert schema["$defs"]["Node"]["required"] == ["name", "children"]


def test_supported_constraints_are_kept_as_is() -> None:
    class Constrained(BaseModel):
        code: str = Field(pattern="^[A-Z]+$", min_length=1, max_length=5)
        email: str = Field(json_schema_extra={"format": "email"})
        score: float = Field(ge=0, lt=10, multiple_of=0.5)
        tags: list[str] = Field(min_length=1, max_length=3)

    properties = strict(Constrained)["properties"]

    assert properties["code"]["pattern"] == "^[A-Z]+$"
    assert properties["code"]["minLength"] == 1
    assert properties["code"]["maxLength"] == 5
    assert properties["email"]["format"] == "email"
    assert properties["score"]["minimum"] == 0
    assert properties["score"]["exclusiveMaximum"] == 10
    assert properties["score"]["multipleOf"] == 0.5
    assert properties["tags"]["minItems"] == 1
    assert properties["tags"]["maxItems"] == 3


def test_the_input_schema_is_not_modified() -> None:
    schema = Defaults.model_json_schema()

    strict_schema(schema)

    assert schema == Defaults.model_json_schema()


# Unsupported constructs


class Pet(BaseModel):
    kind: Literal["pet"]


class Plant(BaseModel):
    kind: Literal["plant"]


class Tagged(BaseModel):
    counts: dict[str, int]


class Loose(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str


class Anything(BaseModel):
    value: Any


class Pair(BaseModel):
    pair: tuple[int, str]


class PetOrPlant(BaseModel):
    item: Pet | Plant = Field(discriminator="kind")


@pytest.mark.parametrize(
    ("model", "construct"),
    [
        (Tagged, r"a free-form object \(such as a dict\) \(at counts\)"),
        (Loose, r"a free-form object \(such as a dict\) \(at the root\)"),
        (Anything, r"a value of any type \(at value\)"),
        (PetOrPlant, r"'oneOf' \(at item\)"),
        (Pair, r"'prefixItems' \(at pair\)"),
    ],
)
def test_unsupported_model_constructs_are_named_with_their_location(
    model: type[BaseModel], construct: str
) -> None:
    with pytest.raises(UnsupportedCapabilityError, match=construct):
        strict(model)


@pytest.mark.parametrize(
    "keyword",
    [
        "allOf",
        "not",
        "if",
        "then",
        "else",
        "dependentRequired",
        "dependentSchemas",
        "patternProperties",
        "prefixItems",
    ],
)
def test_unsupported_keywords_are_rejected(keyword: str) -> None:
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string", keyword: {"secret": "value"}}},
    }

    with pytest.raises(UnsupportedCapabilityError, match=f"'{keyword}'") as caught:
        strict_schema(schema)

    assert "secret" not in str(caught.value)


def test_a_root_that_is_not_an_object_is_rejected() -> None:
    with pytest.raises(UnsupportedCapabilityError, match="root that is not an object"):
        strict_schema({"type": "array", "items": {"type": "string"}})


# Dropping the nulls that stand for defaults


def test_nulls_for_defaulted_fields_validate_to_the_defaults() -> None:
    reply = {"points": None, "n": None, "x": 1}

    assert from_reply(Defaults, reply) == Defaults(points=[], n=3, x=1)


def test_an_originally_nullable_field_keeps_an_explicit_null() -> None:
    reply = {"points": ["a"], "n": 4, "x": None}

    assert from_reply(Defaults, reply) == Defaults(points=["a"], n=4, x=None)


def test_nulls_are_dropped_in_nested_lists_and_recursive_models() -> None:
    reply = {"name": "root", "children": [{"name": "leaf", "children": None}]}

    assert from_reply(Node, reply) == Node(name="root", children=[Node(name="leaf")])


def test_nulls_for_required_fields_are_kept_for_validation_to_reject() -> None:
    reply = {"x": None, "y": 1}

    assert drop_defaulted_nulls(reply, Point.model_json_schema()) == reply
