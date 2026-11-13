"""The built-in router: a model picks the skill and extracts its arguments.

Each routed message takes two structured model calls, both charged to the run
through ``RoutingContext.generate``:

1. **Select:** choose one of the eligible skills, or ask a question.
2. **Extract:** fill in that skill's arguments, or ask a question.

Skill descriptions go only in trusted ``system`` instructions; the caller's
text is sent as untrusted ``user`` messages and the agent's earlier questions
as ``assistant`` messages. The router never guesses: anything ambiguous,
missing or invalid becomes a ``Clarification``.
"""

import asyncio
import copy
from collections.abc import Iterable, Sequence
from typing import Any, Literal

from a2a.helpers import get_message_text
from a2a.types import Message, Role
from pydantic import (
    BaseModel,
    PydanticUserError,
    ValidationError,
    create_model,
)

from agentframe.authoring.definitions import SkillDefinition
from agentframe.errors import DefinitionError, InputValidationError, ProviderError
from agentframe.hosting.routing import Clarification, RoutingContext, SkillCall
from agentframe.models.base import Message as ModelMessage
from agentframe.models.base import Model, ModelSettings
from agentframe.runtime.generation import input_token_limit, output_schema
from agentframe.runtime.limits import RunLimits

NO_TEXT_QUESTION = "Please describe in text what you would like me to do."
"""Asked when the caller's latest message has no text to route."""

UNCLEAR_REQUEST_QUESTION = "Could you tell me more about what you need?"
"""Asked when the model neither chose a skill nor asked its own question."""

NULL_SCHEMA: dict[str, Any] = {"type": "null"}

SELECTION_INSTRUCTIONS = """\
You route a request to one of an agent's skills. The skills are:

{skills}

Read the conversation and choose the one skill that clearly matches the
caller's request: set "skill" to its id and "question" to null.
If the request is ambiguous, matches no skill, or is not a request at all,
set "skill" to null and "question" to one short question for the caller.
Never guess."""

EXTRACTION_INSTRUCTIONS = """\
The caller's request is for this skill:

{skill}

Fill in the skill's arguments using only values the caller stated in the
conversation. Never invent, assume or guess a value. Set an optional argument
to null unless the caller gave its value.
If a required value is missing or unclear, set "arguments" to null and
"question" to one short question asking for it. Otherwise set "question"
to null."""


class Selection(BaseModel):
    """The selection call's reply: a skill ID, or a question for the caller.

    ``selection_output`` narrows ``skill`` to the eligible IDs per request.
    """

    skill: str | None
    question: str | None


class Extraction(BaseModel):
    """The extraction call's reply: the skill's arguments, or a question.

    ``extraction_output`` shows the model the skill's argument schema.
    """

    arguments: dict[str, Any] | None
    question: str | None


class ModelRouter:
    """Routes free-text A2A messages with a model, or asks the caller.

    Opt-in: pass it as ``AgentApp(router=...)``. It uses only the model given
    here, never the agent's, and the application owns and closes that model.
    ``AgentApp`` checks the model and the skills' schemas before serving.
    It can only choose among the skills the caller may run, and the runtime
    still validates the arguments and checks permissions and policy before
    the skill runs.

    Args:
        model: The router's model. It must support structured output. Pass
            the agent's model explicitly to share it.
        instructions: Extra trusted instructions for both routing calls,
            such as domain hints. Never put untrusted content here.
        model_settings: Settings for the routing calls. Each field set
            overrides the library's and agent's value.
    """

    def __init__(
        self,
        model: Model,
        *,
        instructions: str | None = None,
        model_settings: ModelSettings | None = None,
    ) -> None:
        self.model = model
        self.instructions = instructions
        self.model_settings = model_settings

    async def route(
        self,
        conversation: Sequence[Message],
        skills: Sequence[SkillDefinition],
        ctx: RoutingContext,
    ) -> SkillCall | Clarification:
        """Select a skill and extract its arguments, or ask a question.

        Args:
            conversation: The task's A2A messages so far, oldest first. Only
                their text parts are used.
            skills: The skills this caller may run.
            ctx: The run's routing context; both model calls are charged to it.

        Returns:
            The selected skill with only the arguments the model supplied, or
            a question when the request is ambiguous, off-topic or incomplete,
            or the extracted arguments are invalid.

        Raises:
            InputValidationError: No skill is available to this caller. No
                model call is made.
            UnsupportedCapabilityError: The router's model cannot produce the
                selection or a skill's argument schema.
            AgentFrameError: Other errors from ``ctx.generate``, such as a
                limit, refusal or provider error.
        """
        if not skills:
            raise InputValidationError("No skill is available to this caller")
        if not get_message_text(conversation[-1]).strip():
            return Clarification(NO_TEXT_QUESTION)
        messages = model_messages(conversation)

        selection = await self._select(messages, skills, ctx)
        if isinstance(selection, Clarification):
            return selection
        return await self._extract(messages, selection, ctx)

    async def _select(
        self,
        messages: list[ModelMessage],
        skills: Sequence[SkillDefinition],
        ctx: RoutingContext,
    ) -> SkillDefinition | Clarification:
        """Ask the model which skill the conversation is for."""
        described = "\n\n".join(describe_skill(skill) for skill in skills)
        reply = await ctx.generate(
            self.model,
            messages,
            instructions=self._with_extra_instructions(
                SELECTION_INSTRUCTIONS.format(skills=described)
            ),
            output=selection_output(skills),
            model_settings=self.model_settings,
        )
        if reply.question or reply.skill is None:
            return ask(reply.question)
        return next(skill for skill in skills if skill.id == reply.skill)

    async def _extract(
        self,
        messages: list[ModelMessage],
        skill: SkillDefinition,
        ctx: RoutingContext,
    ) -> SkillCall | Clarification:
        """Ask the model for the chosen skill's arguments, then validate them."""
        reply = await ctx.generate(
            self.model,
            messages,
            instructions=self._with_extra_instructions(
                EXTRACTION_INSTRUCTIONS.format(skill=describe_skill(skill))
            ),
            output=extraction_output(skill),
            model_settings=self.model_settings,
        )
        if reply.question or reply.arguments is None:
            return ask(reply.question)
        return checked_call(skill, reply.arguments)

    def validate_skills(self, skills: Iterable[SkillDefinition]) -> None:
        """Build the selection and extraction schemas for ``skills`` once.

        ``AgentApp`` calls this when it is constructed, so a skill whose
        arguments have no JSON schema fails before anything is served.
        Whether the model's adapter accepts each schema is only known at the
        first routing call (``UnsupportedCapabilityError``).

        Args:
            skills: Every skill of the hosted agent.

        Raises:
            DefinitionError: A skill's arguments cannot be described as a
                JSON schema.
        """
        skills = list(skills)
        output_schema(selection_output(skills))
        for skill in skills:
            try:
                output_schema(extraction_output(skill))
            except PydanticUserError:
                raise DefinitionError(
                    f"ModelRouter cannot describe the arguments of skill "
                    f"'{skill.id}' as a JSON schema"
                ) from None

    async def validate_model(self, limits: RunLimits) -> None:
        """Check that the router's model can route under ``limits``.

        ``AgentApp`` calls this at server startup. Reading the model's
        capabilities may do I/O, so it is bounded by ``limits.timeout_seconds``.

        Args:
            limits: The limits routing runs under (the agent's).

        Raises:
            DefinitionError: The model does not support structured output, or
                reports no context window while ``max_input_tokens`` is unset.
            ProviderError: The capabilities could not be read, or reading them
                took longer than the timeout.
        """
        try:
            async with asyncio.timeout(limits.timeout_seconds):
                capabilities = await self.model.capabilities()
        except TimeoutError:
            raise ProviderError(
                "Reading the router model's capabilities timed out", transient=True
            ) from None
        if not capabilities.structured_output:
            raise DefinitionError(
                "ModelRouter's model does not support structured output, which "
                "routing needs"
            )
        try:
            input_token_limit(limits, capabilities)
        except DefinitionError as error:
            raise DefinitionError(f"ModelRouter: {error.message}") from None

    def _with_extra_instructions(self, instructions: str) -> str:
        if self.instructions:
            return f"{instructions}\n\n{self.instructions}"
        return instructions


def ask(question: str | None) -> Clarification:
    """Ask the model's question, or a generic one when it gave none."""
    if question and question.strip():
        return Clarification(question.strip())
    return Clarification(UNCLEAR_REQUEST_QUESTION)


def model_messages(conversation: Sequence[Message]) -> list[ModelMessage]:
    """Return the conversation's text as model messages, oldest first.

    The caller's messages become ``user`` messages and the agent's earlier
    questions ``assistant`` messages. Messages without text are left out.
    """
    messages = []
    for message in conversation:
        text = get_message_text(message)
        if not text.strip():
            continue
        if message.role == Role.ROLE_AGENT:
            messages.append(ModelMessage(role="assistant", content=text))
        else:
            messages.append(ModelMessage(role="user", content=text))
    return messages


def describe_skill(skill: SkillDefinition) -> str:
    """Describe a skill for the router's trusted instructions."""
    lines = [f"id: {skill.id}", f"name: {skill.name}"]
    if skill.tags:
        lines.append(f"tags: {', '.join(skill.tags)}")
    lines.append(f"description: {skill.description}")
    return "\n".join(lines)


def selection_output(skills: Sequence[SkillDefinition]) -> type[Selection]:
    """Return the ``Selection`` model: one of ``skills`` by ID, or a question.

    Built per request, so the model can only name an eligible skill.
    """
    skill_ids = tuple(skill.id for skill in skills)
    return create_model(
        "Selection",
        __base__=Selection,
        skill=(Literal[skill_ids] | None, ...),  # type: ignore[arg-type]
        question=(str | None, ...),
    )


def extraction_output(skill: SkillDefinition) -> type[Extraction]:
    """Return the ``Extraction`` model for ``skill``: its arguments, or a question.

    The model sees the skill's argument schema with every optional argument
    nullable and required, so it says ``null`` rather than inventing a
    value. The reply's ``arguments`` is only checked to be an object here:
    ``checked_call`` validates it against the skill, so invalid values
    become a question instead of a failed run.
    """
    arguments = nullable_optionals(skill.arguments_adapter.json_schema())
    # References inside the arguments schema point at the root's $defs.
    definitions = arguments.pop("$defs", None)
    schema: dict[str, Any] = {
        "title": "Extraction",
        "type": "object",
        "properties": {
            "arguments": {"anyOf": [arguments, NULL_SCHEMA]},
            "question": {"anyOf": [{"type": "string"}, NULL_SCHEMA]},
        },
        "required": ["arguments", "question"],
        "additionalProperties": False,
    }
    if definitions:
        schema["$defs"] = definitions

    class SkillExtraction(Extraction):
        # The schema sent to the model is the skill's, while validation of
        # the reply stays loose (see ``Extraction``).
        @classmethod
        def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return copy.deepcopy(schema)

    return SkillExtraction


def nullable_optionals(arguments_schema: dict[str, Any]) -> dict[str, Any]:
    """Make every optional argument required and nullable, without its default.

    The input is not modified. Without the default, the model has no value
    to copy; ``null`` means the caller did not give one.
    """
    schema = copy.deepcopy(arguments_schema)
    required = set(schema.get("required", ()))
    properties = schema.get("properties", {})
    for name, property_schema in properties.items():
        if name in required:
            continue
        property_schema.pop("default", None)
        properties[name] = {"anyOf": [property_schema, NULL_SCHEMA]}
    schema["required"] = list(properties)
    return schema


def checked_call(
    skill: SkillDefinition, arguments: dict[str, Any]
) -> SkillCall | Clarification:
    """Validate extracted ``arguments`` against ``skill``.

    Nulls for optional arguments are dropped first, so the skill's defaults
    apply. Only the values the model supplied are returned.

    Returns:
        The skill call, or a question naming the invalid arguments (never
        their values).
    """
    optional = optional_arguments(skill)
    supplied = {
        name: value
        for name, value in arguments.items()
        if not (value is None and name in optional)
    }
    try:
        validated = skill.arguments_adapter.validate_python(supplied)
    except ValidationError as error:
        return Clarification(invalid_arguments_question(skill, error))
    return SkillCall(skill.id, validated.model_dump(mode="json", exclude_unset=True))


def optional_arguments(skill: SkillDefinition) -> set[str]:
    """Return the names of the skill's arguments that have defaults."""
    schema = skill.arguments_adapter.json_schema()
    return set(schema.get("properties", {})) - set(schema.get("required", ()))


def invalid_arguments_question(skill: SkillDefinition, error: ValidationError) -> str:
    """Ask for valid values of the arguments ``error`` names, without values.

    Only the skill's own argument names are shown, never names the model
    made up.
    """
    names: list[str] = []
    for item in error.errors(include_input=False, include_url=False):
        name = item["loc"][0] if item["loc"] else None
        if name in skill.argument_names and name not in names:
            names.append(name)
    if not names:
        return UNCLEAR_REQUEST_QUESTION
    return f"Could you give valid values for: {', '.join(names)}?"
