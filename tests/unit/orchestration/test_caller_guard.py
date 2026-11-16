"""The open-proxy guard looks only at how each entry's agent is called.

``tests/integration/test_open_proxy_guard.py`` runs it end to end; these
tests cover the identities a hosted test cannot set up cheaply.
"""

from types import SimpleNamespace
from typing import Any

import pytest

from agentframe import (
    APIKeyCredential,
    OnBehalfOf,
    PermissionDeniedError,
    RemoteSkill,
    Service,
    SkillCatalog,
)
from agentframe.orchestration.catalog import check_caller_guard

SERVICE = Service(credential=APIKeyCredential("key"))
ON_BEHALF_OF = OnBehalfOf(scope="api://agents/.default")
HOSTED: Any = SimpleNamespace(hosted=True)
UNPROTECTED: Any = SimpleNamespace(id="plan", requires_permissions=frozenset())


def entry(agent: str) -> RemoteSkill:
    return RemoteSkill(
        name=f"{agent}.find",
        agent=agent,
        skill="find",
        description="",
        tags=(),
        arguments_schema={"type": "object"},
    )


def catalog(identities: dict[str, Any]) -> SkillCatalog:
    entries = [entry(agent) for agent in identities]
    return SkillCatalog(entries, bindings={}, identities=identities)


@pytest.mark.parametrize("identity", [ON_BEHALF_OF, None])
def test_on_behalf_of_and_uncredentialed_agents_pass(identity: Any) -> None:
    built = catalog({"research": identity})

    check_caller_guard(built, built.entries, UNPROTECTED, HOSTED)


def test_one_service_entry_among_others_is_refused_naming_only_the_skill() -> None:
    built = catalog({"research": ON_BEHALF_OF, "billing": SERVICE})

    with pytest.raises(PermissionDeniedError) as raised:
        check_caller_guard(built, built.entries, UNPROTECTED, HOSTED)

    assert str(raised.value).startswith("Skill 'plan' requires no permissions")
    assert "billing" not in str(raised.value)


@pytest.mark.parametrize(
    ("identities", "mode"),
    [
        ({"research": SERVICE}, "Service"),
        ({"research": ON_BEHALF_OF, "billing": ON_BEHALF_OF}, "OnBehalfOf"),
        ({"research": None}, "none"),
        ({}, "none"),
        ({"research": SERVICE, "billing": ON_BEHALF_OF}, "mixed"),
    ],
)
def test_the_identity_mode_summarizes_every_agent(
    identities: dict[str, Any], mode: str
) -> None:
    assert catalog(identities).identity_mode() == mode
