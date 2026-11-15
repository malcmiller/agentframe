"""Turn a model's final response into a skill's text or validated output."""

from pydantic import BaseModel, RootModel, ValidationError

from agentframe.errors import (
    DefinitionError,
    IncompleteOutputError,
    ModelRefusalError,
    OutputValidationError,
    describe_validation_error,
)
from agentframe.models.base import ModelResponse, OutputSchema
from agentframe.usage import FinishReason

TRUNCATED_MESSAGE = "The model's output was cut short at its output token limit"


def text_from(response: ModelResponse) -> str:
    """Return a response's text, or raise when it is not a complete answer.

    Raises:
        IncompleteOutputError: Output stopped at the token limit or is empty.
        ModelRefusalError: The model refused or a content filter blocked it.
    """
    match response.finish_reason:
        case FinishReason.LENGTH:
            raise IncompleteOutputError(TRUNCATED_MESSAGE)
        case FinishReason.REFUSED:
            raise ModelRefusalError("The model refused the request")
        case FinishReason.CONTENT_FILTER:
            raise ModelRefusalError(
                "The model's output was blocked by a content filter"
            )
    if not response.text:
        raise IncompleteOutputError("The model returned no text")
    return response.text


def output_schema(output: type[BaseModel]) -> OutputSchema:
    """Return the schema the model must follow to produce an ``output`` instance.

    Raises:
        DefinitionError: ``output`` is not a ``BaseModel`` subclass, or is a
            ``RootModel``. Wrap lists and other values in a model field.
    """
    is_model = isinstance(output, type) and issubclass(output, BaseModel)
    if not is_model or issubclass(output, RootModel):
        raise DefinitionError(
            "generate(output=...) must be a Pydantic BaseModel subclass (not a "
            "RootModel); wrap lists and other values in a model field"
        )
    return OutputSchema(name=output.__name__, json_schema=output.model_json_schema())


def validate_output[T: BaseModel](text: str, output: type[T]) -> T:
    """Parse and validate the model's JSON ``text`` as an ``output`` instance.

    Raises:
        OutputValidationError: ``text`` is not valid JSON or does not match
            ``output``. The message names field locations, never the text;
            location parts that are not schema property names are masked.
    """
    try:
        return output.model_validate_json(text)
    except ValidationError as error:
        field_names = schema_property_names(output.model_json_schema())
        raise OutputValidationError(
            f"The model's output is not a valid '{output.__name__}': "
            f"{describe_validation_error(error, field_names)}"
        ) from None


def schema_property_names(schema: object) -> set[str]:
    """Return every property name declared anywhere in a JSON ``schema``.

    Error locations show only these names; other parts, such as dictionary
    keys the model chose, are masked.
    """
    names: set[str] = set()
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            names.update(properties)
        for value in schema.values():
            names |= schema_property_names(value)
    elif isinstance(schema, list):
        for item in schema:
            names |= schema_property_names(item)
    return names
