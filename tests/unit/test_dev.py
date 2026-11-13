"""Development wiring: what ``agentframe dev`` passes each agent, and how it is read."""

from pathlib import Path

import pytest

from agentframe import DefinitionError, dev_settings
from agentframe.dev_server import child_environment
from agentframe.project import ProjectAgent

RESEARCH = ProjectAgent("research-agent", "myapp.research:create_app", 8001)
PLANNER = ProjectAgent("planner", "myapp.planner:create_app", 8002)
KEYS = {"research-agent": "research-key", "planner": "planner-key"}


@pytest.fixture
def dev_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTFRAME_DEV", "1")
    monkeypatch.setenv("AGENTFRAME_PUBLIC_URL", "http://127.0.0.1:8002")
    monkeypatch.setenv("AGENTFRAME_DEV_REGISTRY", ".agentframe/agents.yaml")
    monkeypatch.setenv(
        "AGENTFRAME_DEV_API_KEYS", "research-agent=research-key,developer=client-key"
    )
    monkeypatch.setenv("AGENTFRAME_DEV_REMOTE_KEYS", "research-agent=planner-key")


def test_each_agent_accepts_the_others_keys_and_calls_with_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTFRAME_DEV_CLIENT_KEY", "client-key")
    registry = Path("/project/.agentframe/agents.yaml")

    environment = child_environment(
        PLANNER, [RESEARCH, PLANNER], KEYS, "client-key", registry
    )

    assert environment["AGENTFRAME_DEV"] == "1"
    assert environment["AGENTFRAME_PUBLIC_URL"] == "http://127.0.0.1:8002"
    assert environment["AGENTFRAME_DEV_REGISTRY"] == str(registry)
    assert environment["AGENTFRAME_DEV_API_KEYS"] == (
        "research-agent=research-key,developer=client-key"
    )
    assert environment["AGENTFRAME_DEV_REMOTE_KEYS"] == "research-agent=planner-key"
    assert "AGENTFRAME_DEV_CLIENT_KEY" not in environment


def test_without_a_client_key_only_the_agents_are_accepted() -> None:
    environment = child_environment(
        PLANNER, [RESEARCH, PLANNER], KEYS, None, Path("agents.yaml")
    )

    assert environment["AGENTFRAME_DEV_API_KEYS"] == "research-agent=research-key"


@pytest.mark.usefixtures("dev_environment")
async def test_dev_settings_wire_auth_directory_and_remotes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTFRAME_ALLOW_HTTP_HOSTS", "research-agent, planner")

    dev = dev_settings()

    assert dev.public_url == "http://127.0.0.1:8002"
    caller = await dev.auth.authenticate({"X-API-Key": "research-key"})
    assert caller.id == "research-agent"
    developer = await dev.auth.authenticate({"X-API-Key": "client-key"})
    assert developer.id == "developer"
    assert list(dev.remotes) == ["research-agent"]
    credential = dev.remotes["research-agent"].credential
    assert await credential.headers() == {"X-API-Key": "planner-key"}
    assert dev.directory is not None
    # noinspection PyProtectedMember
    assert dev.directory._allow_http_hosts == frozenset({"research-agent", "planner"})
    for key in ("research-key", "client-key", "planner-key"):
        assert key not in repr(dev)


@pytest.mark.usefixtures("dev_environment")
def test_dev_settings_refuse_to_run_outside_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENTFRAME_DEV")

    with pytest.raises(DefinitionError, match="needs AGENTFRAME_DEV=1"):
        dev_settings()


@pytest.mark.usefixtures("dev_environment")
@pytest.mark.parametrize(
    ("variable", "value", "message"),
    [
        ("AGENTFRAME_PUBLIC_URL", "", "AGENTFRAME_PUBLIC_URL is not set"),
        ("AGENTFRAME_DEV_API_KEYS", "", "lists no keys"),
        ("AGENTFRAME_DEV_API_KEYS", "secret-without-name", "name=key pairs"),
        ("AGENTFRAME_DEV_REMOTE_KEYS", "a=secret-1,a=secret-2", "unique names"),
    ],
)
def test_dev_settings_refuse_malformed_variables_without_showing_keys(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str, message: str
) -> None:
    monkeypatch.setenv(variable, value)

    with pytest.raises(DefinitionError, match=message) as error:
        dev_settings()

    assert "secret" not in str(error.value)
