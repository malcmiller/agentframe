"""The remote skill catalog: what registered agents offer, from their cards.

An orchestrator's ``ctx.remote_skills(...)`` builds a ``SkillCatalog`` of
``RemoteSkill`` entries, either from every agent its directory's sources list
(the registry is the allowlist) or from the ``Remote`` parameters it names.
Later stages offer the entries to a model; building a catalog never calls one.
"""

import logging
import re
from collections.abc import (
    Callable,
    Collection,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, field, replace
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
from agentframe.orchestration.card_text import (
    MAX_SCHEMA_BYTES,
    bounded_schema,
    bounded_tags,
    bounded_text,
    schema_size,
)
from agentframe.orchestration.plan import (
    DEFAULT_MAX_STEPS,
    Plan,
    StepGroups,
    check_plan,
    checked_max_steps,
)
from agentframe.runtime.remotes import RunRemotes
from agentframe.runtime.tools import is_permitted
from agentframe.security.principal import Principal
from agentframe.skill_call import SKILL_CALL_EXTENSION_URI

logger = logging.getLogger(__name__)

MAX_ENTRIES = 20
"""The most entries a catalog may hold; more raise rather than truncate."""

_SKILL_ID_PATTERN = re.compile(r"[a-zA-Z0-9_-]{1,64}")
"""Card skill IDs a structured entry accepts: the same safe characters as
AgentFrame skill and tool names, since entry names become model tool names."""

_UNSAFE_TOOL_CHARACTERS = re.compile(r"[^a-zA-Z0-9_-]")

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
            entry, from ``remote_skills(permissions=...)``; empty by default.
            Entries the caller lacks are never shown to a model, and a call
            to one fails the run before anything is sent.
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
        tool_calls_left: Returns the tool calls the run has left, which
            caps a plan's steps; ``None`` for no cap. Read on every check,
            since the run may make tool calls after building the catalog.
    """

    def __init__(
        self,
        entries: Iterable[RemoteSkill],
        bindings: Mapping[str, Remote[Any]],
        max_tokens_per_call: int | None = None,
        tool_calls_left: Callable[[], int] | None = None,
    ) -> None:
        self._entries = tuple(entries)
        self._by_name = {entry.name: entry for entry in self._entries}
        self._bindings = MappingProxyType(dict(bindings))
        self._max_tokens_per_call = max_tokens_per_call
        self._tool_calls_left = tool_calls_left

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

    def get(self, name: str) -> RemoteSkill | None:
        """Return the entry called ``name``, or ``None`` if there is none."""
        return self._by_name.get(name)

    def check(self, plan: Plan, *, max_steps: int = DEFAULT_MAX_STEPS) -> StepGroups:
        """Check that ``plan`` only calls this catalog's entries, correctly.

        Makes no network or model call. It checks: step IDs are unique and
        match ``^[a-z][a-z0-9_]{0,31}$``; every ``use`` names an entry;
        ``arguments`` are given only to structured entries and ``text``
        only, and always, to ``ask`` entries; every reference, placeholder
        and ``after`` names a step, and a ``.field`` is never taken from an
        ``ask`` step's text; there is no dependency cycle; ``result`` names
        a step; the plan has at most ``max_steps`` steps and is at most
        64 KiB as JSON. Literal arguments are checked against the entry's
        schema's top level only: required properties present (literal or
        referenced) and no unknown ones when the schema forbids them. The
        receiving agent validates the rest when the step runs.

        Args:
            plan: The plan to check.
            max_steps: The most steps the plan may have. It never raises the
                limit above the tool calls the run has left, since each step
                is one tool call.

        Returns:
            The plan's steps in groups: each group's steps depend only on
            steps in earlier groups, so they may run in parallel. Steps keep
            their plan order within a group.

        Raises:
            InputValidationError: The plan is invalid. The message names the
                step (by ID, or by position when the ID is malformed) and the
                field, never an argument value.
            DefinitionError: ``max_steps`` is not a whole number above 0.
        """
        return check_plan(plan, self.get, self.step_limit(max_steps))

    def step_limit(self, max_steps: int) -> int:
        """Return ``max_steps``, lowered to the tool calls the run has left.

        Package-internal: ``ctx.plan`` tells the planning model this limit.

        Raises:
            DefinitionError: ``max_steps`` is not a whole number above 0.
        """
        limit = checked_max_steps(max_steps)
        if self._tool_calls_left is not None:
            limit = min(limit, self._tool_calls_left())
        return limit

    def visible_to(self, principal: Principal | None) -> "SkillCatalog":
        """Return a catalog of only the entries ``principal`` may use.

        Package-internal: a planning model sees only these, and checking its
        plan against them makes a hidden entry look exactly like a missing one.
        """
        return SkillCatalog(
            [entry for entry in self._entries if is_permitted(entry, principal)],
            self._bindings,
            self._max_tokens_per_call,
            self._tool_calls_left,
        )

    def binding(self, entry: RemoteSkill) -> Remote[Any] | None:
        """Return the binding for ``entry`` if it is this very catalog's entry.

        Package-internal: remote tools and plan steps send through it.
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
    permissions: Mapping[str, Collection[str]] | None,
) -> SkillCatalog:
    """Build a run's catalog and remember it on ``run``; see
    ``RunContext.remote_skills``."""
    _check_max_tokens(max_tokens_per_call)
    required = _checked_permissions(permissions)
    names = _checked_collection("agents", agents)
    wanted_tags = _checked_collection("tags", tags) or frozenset()
    declared = _declared(remotes, names) if remotes else {}
    if remotes:
        candidates = {name: remote.discovered for name, remote in declared.items()}
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
    # Counted before any caller's permissions hide entries, so whether a
    # catalog is too large never depends on who calls.
    _check_size(entries)
    _check_not_tool_names(required, entries)
    if remotes:
        cards = {name: candidates[name].card for name in declared}
        _check_permission_names(required, cards, ask)
    _warn_unmatched(run, required, entries)
    entries = [_with_permissions(entry, required) for entry in entries]
    catalog = SkillCatalog(entries, bindings, max_tokens_per_call, run.tool_calls_left)
    run.catalogs.append(catalog)
    return catalog


def _checked_permissions(
    permissions: Mapping[str, Collection[str]] | None,
) -> dict[str, frozenset[str]]:
    """Return ``permissions`` as frozen sets by entry name.

    Raises:
        DefinitionError: ``permissions`` is not a mapping of entry names to
            lists of permission strings.
    """
    if permissions is None:
        return {}
    problem = DefinitionError(
        "remote_skills permissions= must map entry names to lists of "
        'permission strings, for example {"billing-agent.refund": '
        '{"refunds:write"}}'
    )
    if not isinstance(permissions, Mapping):
        raise problem
    checked: dict[str, frozenset[str]] = {}
    for name, required in permissions.items():
        # A bare string would otherwise be read as one permission per character.
        if (
            not isinstance(name, str)
            or isinstance(required, str)
            or not isinstance(required, Collection)
            or not all(isinstance(permission, str) for permission in required)
        ):
            raise problem
        checked[name] = frozenset(required)
    return checked


def _with_permissions(
    entry: RemoteSkill, permissions: Mapping[str, frozenset[str]]
) -> RemoteSkill:
    """Return ``entry`` with its permissions from ``permissions``.

    An ``ask`` entry also requires every permission of its agent's skills,
    since free text can ask the agent for anything its skills do. That holds
    even for skills narrowing left out. A key of another agent whose name
    starts with ``"<agent>."`` adds its permissions too, which over-protects
    but never under-protects.
    """
    required = permissions.get(entry.name, frozenset())
    if entry.skill is None:
        prefix = f"{entry.agent}."
        for name, skill_permissions in permissions.items():
            if name.startswith(prefix):
                required |= skill_permissions
    if not required:
        return entry
    return replace(entry, requires_permissions=required)


def safe_tool_name(name: str) -> str:
    """Return ``name`` with every character other than letters, digits, ``_``
    and ``-`` replaced by ``_``, as model tool names need."""
    return _UNSAFE_TOOL_CHARACTERS.sub("_", name)


def _check_not_tool_names(
    permissions: Mapping[str, frozenset[str]], entries: Sequence[RemoteSkill]
) -> None:
    """Refuse a ``permissions=`` key that is an entry's tool name, not its name.

    Raises:
        DefinitionError: A key is the model tool name of an entry whose own
            name is different, so it would protect nothing.
    """
    names = {entry.name for entry in entries}
    for entry in entries:
        tool = safe_tool_name(entry.name)
        if tool in permissions and tool not in names:
            raise DefinitionError(
                f"remote_skills permissions= names '{tool}', the model tool "
                f"name; use the entry name '{entry.name}'."
            )


def _check_permission_names(
    permissions: Mapping[str, frozenset[str]],
    cards: Mapping[str, AgentCard],
    ask: bool,
) -> None:
    """Refuse a ``permissions=`` key of a declared agent that names no entry.

    A declared agent's entries are fixed by its card, so a key for that
    agent (its name, or its name followed by ``.``) that matches none of
    them is a mistake that would leave an entry unprotected. Entries that
    ``skill=`` or ``tags=`` narrowing left out still count as entries.

    Raises:
        DefinitionError: A key belongs to a declared agent but names none of
            its card's entries.
    """
    for key in permissions:
        owners = [a for a in cards if key == a or key.startswith(f"{a}.")]
        names = {name for a in owners for name in _possible_names(a, cards[a], ask)}
        if owners and key not in names:
            raise DefinitionError(
                f"remote_skills permissions= names '{key}', which is not an "
                "entry of the declared agent; use an entry name such as "
                "'agent.skill', or the agent's name for its ask entry."
            )


def _possible_names(agent: str, card: AgentCard, ask: bool) -> set[str]:
    """Return the entry names the card gives without ``skill=``/``tags=``."""
    names: set[str] = set()
    has_extension = _skill_schemas(card) is not None
    if has_extension:
        names = {f"{agent}.{card_skill.id}" for card_skill in card.skills}
    if not has_extension or ask:
        names.add(agent)
    return names


def _warn_unmatched(
    run: RunRemotes,
    permissions: Mapping[str, frozenset[str]],
    entries: Sequence[RemoteSkill],
) -> None:
    """Log one WARNING per ``permissions=`` key that names no catalog entry.

    The key is the author's own text, so it is safe to log. In registry mode
    a key may legitimately match nothing in this run, for example when its
    agent is down; a typo looks the same, hence a warning, not an error.
    """
    present = {entry.name for entry in entries}
    for key in permissions:
        if key not in present:
            logger.warning(
                "remote_skills permissions= key '%s' matched no catalog entry "
                "(invocation_id=%s)",
                key,
                run.correlation.invocation_id,
                extra=run.correlation.log_extra(),
            )


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
    listed, failed = await run.directory.list_available()
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
    results = await run.directory.resolve_each(listed)
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
    identity = run.identity(name, result.card)
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
