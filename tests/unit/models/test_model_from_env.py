"""model_from_env: choosing Ollama or Foundry with environment variables."""

import pytest

from agentframe import DefinitionError, FoundryModel, OllamaModel, model_from_env
from agentframe.models import foundry

FOUNDRY_ENDPOINT = "https://example.openai.azure.com/openai/v1/"


class FakeCredential:
    """An async token credential that only records being closed."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide model variables a developer may have set for the provider tests."""
    for name in (
        "MODEL_PROVIDER",
        "OLLAMA_MODEL",
        "OLLAMA_ENDPOINT",
        "FOUNDRY_ENDPOINT",
        "FOUNDRY_DEPLOYMENT",
        "MODEL_CONTEXT_WINDOW",
    ):
        monkeypatch.delenv(f"AGENTFRAME_{name}", raising=False)


def use_foundry(monkeypatch: pytest.MonkeyPatch, prefix: str = "AGENTFRAME_") -> None:
    monkeypatch.setenv(f"{prefix}MODEL_PROVIDER", "foundry")
    monkeypatch.setenv(f"{prefix}FOUNDRY_ENDPOINT", FOUNDRY_ENDPOINT)
    monkeypatch.setenv(f"{prefix}FOUNDRY_DEPLOYMENT", "gpt-test")


async def test_ollama_uses_the_local_server_and_a_modest_context_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTFRAME_MODEL_PROVIDER", "ollama")
    monkeypatch.setenv("AGENTFRAME_OLLAMA_MODEL", "llama3.2:3b")

    async with model_from_env() as model:
        assert isinstance(model, OllamaModel)
        assert model.model == "llama3.2:3b"
        assert model.endpoint == "http://localhost:11434"
        assert model._configured_context_window == 8192


async def test_ollama_endpoint_and_context_window_can_be_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTFRAME_MODEL_PROVIDER", "ollama")
    monkeypatch.setenv("AGENTFRAME_OLLAMA_MODEL", "llama3.2:3b")
    monkeypatch.setenv("AGENTFRAME_OLLAMA_ENDPOINT", "http://gpu-box:11434")
    monkeypatch.setenv("AGENTFRAME_MODEL_CONTEXT_WINDOW", "16384")

    async with model_from_env() as model:
        assert isinstance(model, OllamaModel)
        assert model.endpoint == "http://gpu-box:11434"
        assert model._configured_context_window == 16384


async def test_foundry_uses_the_given_credential_and_leaves_it_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_foundry(monkeypatch)
    monkeypatch.setenv("AGENTFRAME_MODEL_CONTEXT_WINDOW", "128000")
    credential = FakeCredential()

    async with model_from_env(credential=credential) as model:  # type: ignore[arg-type]
        assert isinstance(model, FoundryModel)
        assert model.deployment == "gpt-test"
        assert (await model.capabilities()).context_window == 128000

    assert not credential.closed


async def test_foundry_signs_in_with_its_own_default_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_foundry(monkeypatch)
    created: list[FakeCredential] = []

    def default_credential() -> FakeCredential:
        created.append(FakeCredential())
        return created[-1]

    monkeypatch.setattr(foundry, "DefaultAzureCredential", default_credential)

    async with model_from_env() as model:
        assert isinstance(model, FoundryModel)
        assert (await model.capabilities()).context_window is None
        assert not created[0].closed

    assert created[0].closed


async def test_a_prefix_selects_another_set_of_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_foundry(monkeypatch, prefix="PLANNER_")

    async with model_from_env("PLANNER_", credential="test-key-not-real") as model:
        assert isinstance(model, FoundryModel)


@pytest.mark.parametrize(
    ("provider", "missing"),
    [
        (None, "AGENTFRAME_MODEL_PROVIDER"),
        ("ollama", "AGENTFRAME_OLLAMA_MODEL"),
        ("foundry", "AGENTFRAME_FOUNDRY_ENDPOINT"),
        ("foundry", "AGENTFRAME_FOUNDRY_DEPLOYMENT"),
    ],
)
def test_a_missing_or_empty_variable_is_named(
    monkeypatch: pytest.MonkeyPatch, provider: str | None, missing: str
) -> None:
    if provider == "foundry":
        use_foundry(monkeypatch)
    elif provider is not None:
        monkeypatch.setenv("AGENTFRAME_MODEL_PROVIDER", provider)
    monkeypatch.setenv(missing, "")

    with pytest.raises(DefinitionError, match=missing):
        model_from_env(credential="test-key-not-real")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AGENTFRAME_MODEL_PROVIDER", "secret-provider"),
        ("AGENTFRAME_MODEL_CONTEXT_WINDOW", "-5"),
        ("AGENTFRAME_MODEL_CONTEXT_WINDOW", "00"),
        ("AGENTFRAME_MODEL_CONTEXT_WINDOW", "8k"),
    ],
)
def test_an_invalid_value_is_refused_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv("AGENTFRAME_MODEL_PROVIDER", "ollama")
    monkeypatch.setenv("AGENTFRAME_OLLAMA_MODEL", "llama3.2:3b")
    monkeypatch.setenv(name, value)

    with pytest.raises(DefinitionError, match=name) as raised:
        model_from_env()

    assert value not in str(raised.value)
