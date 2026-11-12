import pytest

from agentframe import Agent, DefinitionError, RunContext, RunLimits, agent, skill


@agent(version="1.0.0")
class Writer(Agent):
    """Writes short texts."""

    @skill(id="draft", tags=["text"], requires_permissions=["write"])
    async def draft_outline(self, topic: str, ctx: RunContext) -> str:
        """Draft an outline for a topic."""
        return topic


def test_agent_name_and_description_default_to_class() -> None:
    definition = Writer.definition()

    assert definition.name == "Writer"
    assert definition.description == "Writes short texts."
    assert definition.version == "1.0.0"


def test_skill_defaults_come_from_the_method() -> None:
    draft = Writer.definition().skills["draft"]

    assert draft.name == "Draft Outline"
    assert draft.description == "Draft an outline for a topic."
    assert draft.tags == ("text",)
    assert draft.requires_permissions == frozenset({"write"})
    assert draft.argument_names == ("topic",)


def test_explicit_values_override_defaults() -> None:
    @agent(version="2.0.0", name="Editor", description="Edits texts.")
    class Writer2(Agent):
        """Ignored docstring."""

        @skill(id="edit", name="Edit Text", description="Edits text.")
        async def edit(self, text: str, ctx: RunContext) -> str:
            """Ignored docstring."""
            return text

    definition = Writer2.definition()
    assert (definition.name, definition.description) == ("Editor", "Edits texts.")
    edit = definition.skills["edit"]
    assert (edit.name, edit.description) == ("Edit Text", "Edits text.")


def test_skill_tags_and_permissions_default_to_empty() -> None:
    @agent(version="1.0.0")
    class Plain(Agent):
        """Plain agent."""

        @skill(id="noop")
        async def noop(self, ctx: RunContext) -> None:
            """Do nothing."""

    noop = Plain.definition().skills["noop"]
    assert noop.tags == ()
    assert noop.requires_permissions == frozenset()


def test_missing_description_and_docstring_is_rejected() -> None:
    # Agent's own docstring must not count as an inherited description.
    with pytest.raises(DefinitionError, match="description"):

        @agent(version="1.0.0")
        class Undescribed(Agent):
            @skill(id="noop")
            async def noop(self, ctx: RunContext) -> None:
                """Do nothing."""


def test_skill_without_description_or_docstring_is_rejected() -> None:
    with pytest.raises(DefinitionError, match="description"):

        @agent(version="1.0.0")
        class Quiet(Agent):
            """Has an undocumented skill."""

            @skill(id="noop")
            async def noop(self, ctx: RunContext) -> None:
                pass


def test_missing_version_is_rejected() -> None:
    with pytest.raises(TypeError):
        agent()  # type: ignore[call-arg]


def test_sync_skill_is_rejected() -> None:
    with pytest.raises(DefinitionError, match="'publish' is not async"):

        @agent(version="1.0.0")
        class SyncWriter(Agent):
            """Writes synchronously."""

            @skill(id="publish")
            def publish(self, ctx: RunContext) -> None:
                """Publish."""


def test_skill_without_ctx_is_rejected() -> None:
    with pytest.raises(DefinitionError, match="ctx"):

        @agent(version="1.0.0")
        class NoContext(Agent):
            """Has no ctx."""

            @skill(id="echo")
            async def echo(self, text: str) -> str:
                """Echo."""
                return text


def test_ctx_must_be_annotated_run_context() -> None:
    with pytest.raises(DefinitionError, match="RunContext"):

        @agent(version="1.0.0")
        class WrongContext(Agent):
            """Has a wrong ctx type."""

            @skill(id="echo")
            async def echo(self, text: str, ctx: str) -> str:
                """Echo."""
                return text


def test_unannotated_parameter_is_rejected() -> None:
    with pytest.raises(DefinitionError, match="'text' needs a type"):

        @agent(version="1.0.0")
        class Untyped(Agent):
            """Has an untyped parameter."""

            @skill(id="echo")
            async def echo(self, text, ctx: RunContext) -> str:  # type: ignore[no-untyped-def]
                """Echo."""
                return str(text)


def test_missing_return_annotation_is_rejected() -> None:
    with pytest.raises(DefinitionError, match="return annotation"):

        @agent(version="1.0.0")
        class NoReturnType(Agent):
            """Has no return type."""

            @skill(id="echo")
            async def echo(self, text: str, ctx: RunContext):  # type: ignore[no-untyped-def]
                """Echo."""
                return text


def test_unsupported_annotation_is_rejected() -> None:
    class Opaque:
        pass

    with pytest.raises(DefinitionError, match="unsupported type annotation"):

        @agent(version="1.0.0")
        class Strange(Agent):
            """Takes an opaque argument."""

            @skill(id="take")
            async def take(self, thing: Opaque, ctx: RunContext) -> None:
                """Take a thing."""


def test_parameter_named_principal_is_rejected() -> None:
    with pytest.raises(DefinitionError, match="reserved"):

        @agent(version="1.0.0")
        class Impersonator(Agent):
            """Takes a principal argument."""

            @skill(id="act")
            async def act(self, principal: str, ctx: RunContext) -> str:
                """Act as someone."""
                return principal


def test_duplicate_skill_ids_are_rejected() -> None:
    with pytest.raises(DefinitionError, match="'echo' is used by both"):

        @agent(version="1.0.0")
        class Twice(Agent):
            """Declares one id twice."""

            @skill(id="echo")
            async def first(self, ctx: RunContext) -> None:
                """First."""

            @skill(id="echo")
            async def second(self, ctx: RunContext) -> None:
                """Second."""


def test_subclass_inherits_skills() -> None:
    @agent(version="1.1.0")
    class SeniorWriter(Writer):
        """Writes and reviews."""

        @skill(id="review")
        async def review(self, text: str, ctx: RunContext) -> str:
            """Review a text."""
            return text

    assert set(SeniorWriter.definition().skills) == {"draft", "review"}


def test_subclass_can_redeclare_a_skill() -> None:
    @agent(version="1.1.0")
    class CarefulWriter(Writer):
        """Writes carefully."""

        @skill(id="draft", limits=RunLimits(timeout_seconds=5))
        async def draft_outline(self, topic: str, ctx: RunContext) -> str:
            """Draft a careful outline."""
            return topic

    draft = CarefulWriter.definition().skills["draft"]
    assert draft.description == "Draft a careful outline."
    assert draft.limits == RunLimits(timeout_seconds=5)


def test_overriding_a_skill_without_skill_is_rejected() -> None:
    with pytest.raises(DefinitionError, match="without @skill"):

        @agent(version="1.1.0")
        class SneakyWriter(Writer):
            """Overrides a skill silently."""

            async def draft_outline(self, topic: str, ctx: RunContext) -> str:
                return topic


def test_undecorated_class_has_no_definition() -> None:
    class Bare(Agent):
        """Not decorated."""

    with pytest.raises(DefinitionError, match="not decorated"):
        Bare.definition()


def test_undecorated_subclass_does_not_reuse_parent_definition() -> None:
    class Child(Writer):
        """Not decorated."""

    with pytest.raises(DefinitionError, match="not decorated"):
        Child.definition()


def test_agent_without_skills_is_rejected() -> None:
    with pytest.raises(DefinitionError, match="no skills"):

        @agent(version="1.0.0")
        class Empty(Agent):
            """Has nothing to offer."""


def test_permissions_must_not_be_a_single_string() -> None:
    with pytest.raises(DefinitionError, match="lists of strings"):
        skill(id="admin", requires_permissions="admin")


def test_run_limits_require_a_positive_timeout() -> None:
    with pytest.raises(DefinitionError, match="greater than 0"):
        RunLimits(timeout_seconds=0)
