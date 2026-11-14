"""The remote skill catalog: what registered agents offer, from their cards.

An orchestrator's ``ctx.remote_skills(...)`` builds a ``SkillCatalog`` of
``RemoteSkill`` entries, either from every agent its directory's sources list
(the registry is the allowlist) or from the ``Remote`` parameters it names.
Later stages offer the entries to a model; building a catalog never calls one.
"""

import logging
import re
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from a2a.types import AgentCard, AgentSkill

# protobuf ships no type stubs.
from google.protobuf.json_format import MessageToDict  # type: ignore[import-untyped]

from agentframe.discovery.cards import InvalidCardError
from agentframe.discovery.directory import DiscoveredAgent
from agentframe.discovery.identity import check_identity
from agentframe.discovery.registry import AgentReference
from agentframe.discovery.remote import Remote, check_no_redirects
from agentframe.errors import AgentFrameError, DefinitionError, RemoteAgentError
from agentframe.hosting.skill_call import SKILL_CALL_EXTENSION_URI
from agentframe.orchestration.card_text import (
    MAX_SCHEMA_BYTES,
    bounded_schema,
    bounded_tags,
    bounded_text,
    schema_size,
)
from agentframe.runtime.remotes import RunRemotes

logger = logging.getLogger(__name__)

MAX_ENTRIES = 20
"""The most entries a catalog may hold; more raise rather than truncate."""

_SKILL_ID_PATTERN = re.compile(r"[a-zA-Z0-9_-]{1,64}")
"""Card skill IDs a structured entry accepts: the same safe characters as
AgentFrame skill and tool names, since entry names become model tool names."""

CARD_UNAVAILABLE = "card unavailable"
CARD_INVALID = "card invalid"
SECURITY_NOT_SATISFIED = "security not satisfied"
SCHEMA_TOO_LARGE = "schema too large"


@dataclass(frozen=True)
class RemoteSkill:
    """One thing an orchestrator's model may choose: a remote skill or ``ask``.

    Every text field comes from another agent's card, bounded but untrusted:
    pass it to a model as data, never as instructions.

    Attributes:
        name: ``"agent.skill"`` for a structured skill, or the agent's name
            for a free-text ``ask`` entry, for example
            ``"research-agent.find_sources"``.
        agent: The agent's name in the directory.
        skill: The skill's ID on the card, or ``None`` for an ``ask`` entry.
        description: The card's description of the skill (of the agent, for
            ``ask``), without control characters and at most 500 characters.
        tags: At most ten of the card's tags, bounded the same way.
        arguments_schema: The JSON Schema of the skill's arguments, from the
            card's skill-call extension, with its ``description`` strings
            bounded and its ``examples`` dropped; ``None`` for ``ask``.
            Treat it as read-only.
        requires_permissions: Permissions a caller must hold to use the
            entry. Empty; set by later stories.
    """

    name: str
    agent: str
    skill: str | None
    description: str
    tags: tuple[str, ...]
    arguments_schema: dict[str, Any] | None = field(default=None, hash=False)
    requires_permissions: frozenset[str] = frozenset()


class SkillCatalog:
    """An immutable list of ``RemoteSkill`` entries and the run's bindings.

    Returned by ``RunContext.remote_skills``; not meant to be constructed by
    application code. Iterate it, or use ``entries``. It belongs to the run
    that built it, since each agent's binding carries that run's deadline,
    depth, token budget and identity.

    Args:
        entries: The catalog's entries, in order.
        bindings: The run's binding to each entry's agent, by agent name.
        max_tokens_per_call: The tokens each call to an entry may use, or
            ``None`` for everything the run has left.
    """

    def __init__(
        self,
        entries: Iterable[RemoteSkill],
        bindings: Mapping[str, Remote[Any]],
        max_tokens_per_call: int | None = None,
    ) -> None:
        self._entries = tuple(entries)
        self._bindings = MappingProxyType(dict(bindings))
        self._max_tokens_per_call = max_tokens_per_call

    @property
    def entries(self) -> tuple[RemoteSkill, ...]:
        """The catalog's entries, in source and card order."""
        return self._entries

    def __iter__(self) -> Iterator[RemoteSkill]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        return f"SkillCatalog({[entry.name for entry in self._entries]!r})"

    @property
    def max_tokens_per_call(self) -> int | None:
        """The tokens each call to an entry may use; ``None`` means everything
        the run has left."""
        return self._max_tokens_per_call

    def _binding(self, entry: RemoteSkill) -> Remote[Any] | None:
        """Return the binding for ``entry`` if it is this very catalog's entry.

        Matched by identity, not equality, so an entry from another run's
        catalog, or one built by hand, is never bound to this run.
        """
        if any(own is entry for own in self._entries):
            return self._bindings[entry.agent]
        return None


class _UnusableCardError(Exception):
    """A card that was fetched but cannot contribute entries; ``args[0]`` is
    the reason."""


async def build_catalog(
    run: RunRemotes,
    remotes: Sequence[Remote[Any]],
    *,
    agents: Collection[str] | None,
    skill: str | None,
    tags: Collection[str],
    ask: bool,
    max_tokens_per_call: int | None,
) -> SkillCatalog:
    """Build a run's catalog and remember it on ``run``; see
    ``RunContext.remote_skills``."""
    _check_max_tokens(max_tokens_per_call)
    names = _checked_collection("agents", agents)
    wanted_tags = _checked_collection("tags", tags) or frozenset()
    declared = _declared(remotes, names) if remotes else {}
    if remotes:
        # noinspection PyProtectedMember
        candidates = {name: remote._found for name, remote in declared.items()}
    else:
        candidates = await _registered(run, names)
    entries: list[RemoteSkill] = []
    bindings: dict[str, Remote[Any]] = {}
    for name, found in candidates.items():
        try:
            agent_entries = _entries(name, found.card, skill, wanted_tags, ask)
        except _UnusableCardError as unusable:
            if remotes:  # Named by the author, so raised as in M8+.
                raise RemoteAgentError(
                    f"Agent '{name}' cannot be used in a skill catalog: "
                    f"{unusable.args[0]}"
                ) from None
            _warn_skipped(run, name, unusable.args[0])
            continue
        if not agent_entries:
            continue
        entries += agent_entries
        bindings[name] = declared[name] if remotes else run.bind_found(found)
    _check_unique_names(entries)
    _check_size(entries)
    catalog = SkillCatalog(entries, bindings, max_tokens_per_call)
    run.catalogs.append(catalog)
    return catalog


def _check_max_tokens(max_tokens_per_call: int | None) -> None:
    # bool is an int subclass, but True is not a token count.
    if max_tokens_per_call is not None and (
        not isinstance(max_tokens_per_call, int)
        or isinstance(max_tokens_per_call, bool)
        or max_tokens_per_call < 1
    ):
        raise DefinitionError(
            "remote_skills max_tokens_per_call= must be a whole number "
            "greater than 0, or None"
        )


def _declared(
    remotes: Sequence[Remote[Any]], names: frozenset[str] | None
) -> dict[str, Remote[Any]]:
    """Return the skill's ``Remote`` parameters, narrowed to ``names``.

    Raises:
        DefinitionError: A positional argument is not a ``Remote``, or
            ``names`` holds a name none of them has.
    """
    if not all(isinstance(remote, Remote) for remote in remotes):
        raise DefinitionError(
            "remote_skills takes the skill's Remote parameters as positional "
            "arguments, or none for every registered agent."
        )
    declared = {remote.name: remote for remote in remotes}
    if names is None:
        return declared
    _check_known(names, declared)
    return {name: remote for name, remote in declared.items() if name in names}


async def _registered(
    run: RunRemotes, names: frozenset[str] | None
) -> dict[str, DiscoveredAgent]:
    """Return every registered agent the run can call, skipping the others.

    An agent is skipped, with a warning, when its card cannot be fetched or
    is invalid, or its card's security cannot be satisfied; a source that
    cannot list its agents is skipped the same way. The agent the run is
    hosted as is left out silently.

    Raises:
        DefinitionError: No source lists a name in ``names`` (unless a
            source failed), a name is listed twice, the directory is not
            open, or credentials would be sent through a client that follows
            redirects.
    """
    # noinspection PyProtectedMember
    listed, failed = await run.directory._list_available()
    for position, source in failed:
        logger.warning(
            "Skill catalog skipped source %d (%s): it could not list its agents",
            position,
            type(source).__name__,
            extra=run.correlation.log_extra(),
        )
    if names is not None:
        if not failed:  # A missing name may belong to a source that failed.
            _check_known(names, {reference.name for _, reference in listed})
        listed = [entry for entry in listed if entry[1].name in names]
    # noinspection PyProtectedMember
    results = await run.directory._resolve_each(listed)
    usable: dict[str, DiscoveredAgent] = {}
    for (_, reference), result in zip(listed, results, strict=True):
        found = _usable(run, reference, result)
        if found is not None:
            usable[reference.name] = found
    return usable


def _usable(
    run: RunRemotes,
    reference: AgentReference,
    result: DiscoveredAgent | AgentFrameError,
) -> DiscoveredAgent | None:
    """Return the agent if the run may call it, or ``None`` to skip it."""
    name = reference.name
    if isinstance(result, AgentFrameError):
        reason = CARD_INVALID if isinstance(result, InvalidCardError) else None
        _warn_skipped(run, name, reason or CARD_UNAVAILABLE)
        return None
    if result.interface_url == run.own_interface_url:
        return None  # The orchestrator itself.
    identity = run.identity(name)
    try:
        check_identity(name, result.card, identity)
    except DefinitionError:
        _warn_skipped(run, name, SECURITY_NOT_SATISFIED)
        return None
    check_no_redirects(run.directory, name, identity)
    return result


def _entries(
    agent: str,
    card: AgentCard,
    skill: str | None,
    tags: frozenset[str],
    ask: bool,
) -> list[RemoteSkill]:
    """Return the entries an agent's card contributes after narrowing.

    An agent with the skill-call extension contributes one entry per
    matching card skill, plus an ``ask`` entry when ``ask`` is set; any
    other agent contributes one ``ask`` entry. Narrowing matches ``skill``
    and all of ``tags`` on the same card skill, as ``AgentDirectory.find``.

    Raises:
        _UnusableCardError: The card has the extension and a skill ID that is
            unsafe or listed twice, or a matching skill's schema is missing
            or too large.
    """
    schemas = _skill_schemas(card)
    if schemas is not None:
        _check_skill_ids(card)
    matching = [s for s in card.skills if _matches(s, skill, tags)]
    narrowed = skill is not None or bool(tags)
    if narrowed and not matching:
        return []
    entries = []
    if schemas is not None:
        entries = [_skill_entry(agent, s, schemas.get(s.id)) for s in matching]
    if schemas is None or ask:
        entries.append(_ask_entry(agent, card))
    return entries


def _check_skill_ids(card: AgentCard) -> None:
    """Refuse card skill IDs that cannot name a unique, safe entry.

    Raises:
        _UnusableCardError: An ID is not 1-64 letters, digits, ``_`` or
            ``-``, or the card lists it twice.
    """
    ids = [card_skill.id for card_skill in card.skills]
    unsafe = any(not _SKILL_ID_PATTERN.fullmatch(skill_id) for skill_id in ids)
    if unsafe or len(set(ids)) != len(ids):
        raise _UnusableCardError(CARD_INVALID)


def _matches(card_skill: AgentSkill, skill: str | None, tags: frozenset[str]) -> bool:
    return (skill is None or card_skill.id == skill) and tags <= set(card_skill.tags)


def _skill_schemas(card: AgentCard) -> dict[str, Any] | None:
    """Return the argument schemas from the card's skill-call extension.

    Returns:
        The schemas by skill ID, or ``None`` when the card does not declare
        the extension (so its skills cannot be called by ID).
    """
    for extension in card.capabilities.extensions:
        if extension.uri == SKILL_CALL_EXTENSION_URI:
            schemas = MessageToDict(extension.params).get("skills")
            return schemas if isinstance(schemas, dict) else {}
    return None


def _skill_entry(agent: str, card_skill: AgentSkill, schema: Any) -> RemoteSkill:
    if not isinstance(schema, dict):
        raise _UnusableCardError(CARD_INVALID)
    bounded = bounded_schema(schema)
    if schema_size(bounded) > MAX_SCHEMA_BYTES:
        raise _UnusableCardError(SCHEMA_TOO_LARGE)
    return RemoteSkill(
        name=f"{agent}.{card_skill.id}",
        agent=agent,
        skill=card_skill.id,
        description=bounded_text(card_skill.description or card_skill.name),
        tags=bounded_tags(card_skill.tags),
        arguments_schema=bounded,
    )


def _ask_entry(agent: str, card: AgentCard) -> RemoteSkill:
    return RemoteSkill(
        name=agent,
        agent=agent,
        skill=None,
        description=bounded_text(card.description),
        tags=bounded_tags(tag for s in card.skills for tag in s.tags),
    )


def _check_known(names: frozenset[str], known: Collection[str]) -> None:
    unknown = sorted(names - set(known))
    if unknown:
        raise DefinitionError(
            f"remote_skills agents= names agents it cannot use: {', '.join(unknown)}"
        )


def _check_unique_names(entries: Sequence[RemoteSkill]) -> None:
    """Refuse two entries with one name, such as agent ``a.b``'s ``ask`` entry
    and agent ``a``'s skill ``b``: plans and tools refer to entries by name.

    Raises:
        DefinitionError: Two entries share a name.
    """
    seen: set[str] = set()
    for entry in entries:
        if entry.name in seen:
            raise DefinitionError(
                f"Two skill catalog entries are named '{entry.name}'; narrow "
                "the catalog with agents= or rename one of the agents."
            )
        seen.add(entry.name)


def _check_size(entries: Sequence[RemoteSkill]) -> None:
    if len(entries) > MAX_ENTRIES:
        raise DefinitionError(
            f"The skill catalog has {len(entries)} entries, more than the "
            f"{MAX_ENTRIES} a model can choose among reliably. Narrow it with "
            "agents=, skill= or tags=, or pass the skill's Remote parameters; "
            "it is never truncated."
        )


def _checked_collection(
    parameter: str, values: Collection[str] | None
) -> frozenset[str] | None:
    # A bare string would otherwise be read as one value per character.
    if isinstance(values, str):
        raise DefinitionError(
            f"remote_skills {parameter}= must be a list, not a single string."
        )
    return None if values is None else frozenset(values)


def _warn_skipped(run: RunRemotes, agent: str, reason: str) -> None:
    # Only the registry's name and a fixed reason: card content and URLs
    # are untrusted or may be private.
    logger.warning(
        "Skill catalog skipped agent '%s': %s (invocation_id=%s)",
        agent,
        reason,
        run.correlation.invocation_id,
        extra=run.correlation.log_extra(),
    )
