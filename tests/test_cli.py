"""``agentframe run``: serve an app factory, or an agent class with ``--dev``."""

import importlib
import logging
import sys
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
import uvicorn

from agentframe import AgentApp, DefinitionError
from agentframe.cli import CommandError, RunSettings, build_app, main
from local_server import ServeLocally


def settings(target: str, *, host: str = "127.0.0.1", dev: bool = False) -> RunSettings:
    return RunSettings(target=target, host=host, port=8000, dev=dev)


@pytest.fixture
def uvicorn_runs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record what ``agentframe run`` asks uvicorn to serve instead of serving it."""
    runs: list[dict[str, Any]] = []
    monkeypatch.setattr(
        uvicorn, "run", lambda app, **options: runs.append({"app": app, **options})
    )
    return runs


def test_factory_returns_the_applications_own_app() -> None:
    app = build_app(settings("cli_targets:create_app"))

    assert isinstance(app, AgentApp)


def test_run_serves_the_factory_app_on_the_default_host_and_port(
    uvicorn_runs: list[dict[str, Any]],
) -> None:
    assert main(["run", "cli_targets:create_app"]) == 0

    [run] = uvicorn_runs
    assert isinstance(run["app"], AgentApp)
    assert (run["host"], run["port"]) == ("127.0.0.1", 8000)


def test_run_passes_host_and_port_to_the_server(
    uvicorn_runs: list[dict[str, Any]],
) -> None:
    main(["run", "cli_targets:create_app", "--host", "0.0.0.0", "--port", "9000"])

    [run] = uvicorn_runs
    assert (run["host"], run["port"]) == ("0.0.0.0", 9000)


def test_reload_worker_rebuilds_the_same_app(
    uvicorn_runs: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Restore the environment afterwards: the command hands the worker its
    # settings through an environment variable.
    monkeypatch.setenv("AGENTFRAME_RUN_SETTINGS", "")
    main(["run", "cli_targets:Echo", "--dev", "--reload", "--port", "9001"])

    [run] = uvicorn_runs
    assert run["reload"] is True
    assert run["factory"] is True
    module_name, _, function_name = run["app"].partition(":")
    rebuild = getattr(importlib.import_module(module_name), function_name)
    assert isinstance(rebuild(), AgentApp)


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("cli_targets", "not an import path"),
        ("cli_targets:", "not an import path"),
        ("no_such_module:create_app", "Cannot import module 'no_such_module'"),
        ("cli_targets:missing", "has no attribute 'missing'"),
        ("cli_targets:not_a_function", "not a function that returns an AgentApp"),
        ("cli_targets:create_nothing", "returned NoneType, not an AgentApp"),
    ],
)
def test_bad_targets_are_reported_clearly(target: str, message: str) -> None:
    with pytest.raises(CommandError, match=message):
        build_app(settings(target))


def test_agent_class_without_dev_suggests_dev() -> None:
    with pytest.raises(CommandError, match=r"returned Echo.*add --dev"):
        build_app(settings("cli_targets:Echo"))


def test_invalid_agent_in_the_factory_fails_with_its_definition_error() -> None:
    with pytest.raises(DefinitionError, match="not decorated with @agent"):
        build_app(settings("cli_targets:create_invalid_app"))


def test_errors_print_one_line_and_exit_with_1(
    capsys: pytest.CaptureFixture[str], uvicorn_runs: list[dict[str, Any]]
) -> None:
    exit_code = main(["run", "cli_targets:create_invalid_app"])

    assert exit_code == 1
    assert capsys.readouterr().err.startswith("agentframe: error: Agent 'Undecorated'")
    assert uvicorn_runs == []


def test_missing_uvicorn_names_the_extra_to_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, "uvicorn", None)

    assert main(["run", "cli_targets:create_app"]) == 1
    assert "pip install 'agentframe[server]'" in capsys.readouterr().err


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_dev_serves_an_agent_class_anonymously_on_localhost(
    host: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="agentframe"):
        app = build_app(settings("cli_targets:Echo", host=host, dev=True))

    assert isinstance(app, AgentApp)
    assert "AllowAnonymous" in caplog.text
    assert "Never use --dev in production" in caplog.text


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "echo.example.com"])
def test_dev_refuses_non_localhost_hosts(host: str) -> None:
    with pytest.raises(CommandError, match="only binds to localhost"):
        build_app(settings("cli_targets:Echo", host=host, dev=True))


@pytest.mark.parametrize(
    ("target", "error", "message"),
    [
        ("cli_targets:create_app", CommandError, "needs an Agent class"),
        ("cli_targets:NeedsSettings", CommandError, "cannot construct NeedsSettings"),
        ("cli_targets:Undecorated", DefinitionError, "not decorated with @agent"),
    ],
)
def test_dev_rejects_unusable_agent_classes(
    target: str, error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        build_app(settings(target, dev=True))


async def test_dev_app_serves_its_card_without_credentials(
    serve: ServeLocally,
) -> None:
    def dev_app(base_url: str) -> AgentApp:
        port = urlsplit(base_url).port
        assert port is not None
        return build_app(RunSettings("cli_targets:Echo", "127.0.0.1", port, dev=True))

    async with serve(dev_app) as base_url, httpx.AsyncClient() as client:
        response = await client.get(f"{base_url}/.well-known/agent-card.json")

    assert response.status_code == 200
    assert response.json()["supportedInterfaces"][0]["url"] == f"{base_url}/a2a"
