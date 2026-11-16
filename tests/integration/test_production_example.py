"""``examples/production.py``: production settings from the environment only.

The factory must refuse to start without its configuration, never fall back
to development settings, and serve Entra-authenticated callers.
"""

import httpx
import pytest
from a2a.helpers import get_artifact_text

from a2a_client import call_skill, connect
from agentframe import AgentApp, DefinitionError, EntraIDAuth
from agentframe.testing import ScriptedModel
from examples import production
from fake_entra import API_CLIENT_ID, TENANT_ID, FakeEntra
from local_server import ServeLocally

REQUIRED = {
    "ASSISTANT_PUBLIC_URL": "https://assistant.example.com",
    "ASSISTANT_ENTRA_TENANT_ID": TENANT_ID,
    "ASSISTANT_ENTRA_AUDIENCE": API_CLIENT_ID,
    "AGENTFRAME_MODEL_PROVIDER": "ollama",
    "AGENTFRAME_OLLAMA_MODEL": "llama3.2:3b",
}


@pytest.mark.parametrize("missing", sorted(REQUIRED))
def test_refuses_to_start_without_each_setting(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    # Development mode changes nothing: there are no development defaults.
    monkeypatch.setenv("AGENTFRAME_DEV", "1")
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(missing)

    with pytest.raises(DefinitionError, match=missing):
        production.create_app()


def test_builds_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)

    # Construction does no I/O; the model is checked at server startup.
    assert isinstance(production.create_app(), AgentApp)


async def test_serves_entra_authenticated_callers(
    serve: ServeLocally, monkeypatch: pytest.MonkeyPatch
) -> None:
    entra = FakeEntra()
    auth = EntraIDAuth(
        tenant_id=TENANT_ID, audience=API_CLIENT_ID, transport=entra.transport
    )

    def make_app(public_url: str) -> AgentApp:
        monkeypatch.setenv("ASSISTANT_PUBLIC_URL", public_url)
        return production.create_app(ScriptedModel(["Paris."]), auth)

    async with serve(make_app) as base_url:
        async with httpx.AsyncClient() as http:
            ready = await http.get(f"{base_url}/ready")
            anonymous = await http.post(f"{base_url}/a2a", json={})
        async with await connect(base_url, entra.token()) as client:
            task = await call_skill(client, "answer", {"question": "France?"})

    assert ready.status_code == 200
    assert anonymous.status_code == 401
    assert get_artifact_text(task.artifacts[0]) == "Paris."
