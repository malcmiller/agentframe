"""The ``agentframe`` command line: ``agentframe run`` serves one agent.

Usage::

    agentframe run myapp.hosting:create_app --host 127.0.0.1 --port 8000
    agentframe run myapp.agents:Echo --dev
    agentframe dev
    agentframe compose
    agentframe eval evals.summaries:create_evaluation --min-pass-rate 0.9

``agentframe dev`` runs every agent listed in ``agentframe.yaml``; see
``agentframe.dev_server``. ``agentframe compose`` writes a development
``compose.yaml`` and ``Dockerfile`` for them; see ``agentframe.compose``.
``agentframe eval`` runs an evaluation and exits non-zero when it misses a
threshold, for CI without pytest.

The first form calls the application's own ``create_app()`` function, which
returns a fully configured ``AgentApp``; secrets belong in environment
variables read inside it, never in command-line flags. The second form is for
local development only: it serves an agent class anonymously on localhost.

On SIGTERM or Ctrl+C the app stops being ready and refuses new messages at
once, waits up to its ``HostConfig.shutdown_grace_seconds`` for running work,
then cancels the rest and exits. With ``--reload`` that shutdown starts only
after Uvicorn has waited for open requests, bounded by the same grace period.
"""

import argparse
import asyncio
import contextlib
import importlib
import json
import logging
import math
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from types import ModuleType

from agentframe.authoring.agent import Agent
from agentframe.compose import write_compose
from agentframe.dev import DEV_VARIABLE
from agentframe.dev_server import DEFAULT_STARTUP_TIMEOUT_SECONDS, DevError, run_dev
from agentframe.errors import AgentFrameError
from agentframe.evaluation import EvalReport, Evaluation
from agentframe.hosting.app import AgentApp
from agentframe.hosting.host import HostConfig, is_loopback_host
from agentframe.project import PROJECT_FILE
from agentframe.security.authentication import AllowAnonymous

logger = logging.getLogger("agentframe")

# Uvicorn's --reload runs the server in a fresh worker process that rebuilds
# the app through an import string, so the parent passes its settings here.
_RELOAD_SETTINGS_VARIABLE = "AGENTFRAME_RUN_SETTINGS"


class CommandError(Exception):
    """A problem the command reports as a one-line message instead of a traceback."""


@dataclass(frozen=True)
class RunSettings:
    """What ``agentframe run`` serves, and where.

    Args:
        target: ``module:function`` (an app factory) or, with ``dev``,
            ``module:AgentClass``.
        host: The address the server binds to.
        port: The port the server binds to.
        dev: Serve an agent class anonymously on localhost for development.
    """

    target: str
    host: str
    port: int
    dev: bool


@dataclass(frozen=True)
class EvalSettings:
    """What ``agentframe eval`` runs, and the thresholds it must meet.

    The thresholds are passed straight to ``EvalReport.require``.

    Args:
        target: ``module:function`` returning an ``Evaluation``.
        json_path: Where to write the JSON report, if anywhere.
        capture_content: Allow an evaluation built with
            ``capture_content=True``; without it such an evaluation is refused
            before it runs.
        min_pass_rate: The lowest acceptable pass rate.
        max_errors: The most errored attempts accepted.
        max_total_tokens: The most tokens the evaluation may use.
        allow_not_run: Accept attempts that never started.
        allow_over_budget: Accept usage over the evaluation's own budget.
    """

    target: str
    json_path: str | None = None
    capture_content: bool = False
    min_pass_rate: float | None = None
    max_errors: int = 0
    max_total_tokens: int | None = None
    allow_not_run: bool = False
    allow_over_budget: bool = False


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``agentframe`` command.

    Args:
        argv: Command-line arguments without the program name. Defaults to
            ``sys.argv[1:]``.

    Returns:
        The process exit code: 0 after a clean shutdown, 1 on a reported error.
        ``agentframe eval`` returns 0 when the evaluation meets its
        thresholds, 1 when it misses one and 2 on a configuration error.
    """
    arguments = _parser().parse_args(argv)
    _configure_logging()
    if arguments.command == "eval":
        return evaluate(_eval_settings(arguments))
    try:
        if arguments.command == "dev":
            run_dev(Path(arguments.config), startup_timeout=arguments.startup_timeout)
        elif arguments.command == "compose":
            _compose(Path(arguments.config), force=arguments.force)
        else:
            settings = RunSettings(
                target=arguments.target,
                host=arguments.host,
                port=arguments.port,
                dev=arguments.dev,
            )
            serve(settings, reload=arguments.reload)
    except (CommandError, DevError, AgentFrameError) as error:
        print(f"agentframe: error: {error}", file=sys.stderr)
        return 1
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentframe")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser(
        "run",
        help="serve one agent over A2A",
        description="Serve one agent over A2A with uvicorn.",
    )
    run.add_argument(
        "target",
        help="module:function returning an AgentApp, or module:AgentClass with --dev",
    )
    run.add_argument("--host", default="127.0.0.1", help="bind address")
    run.add_argument("--port", type=int, default=8000, help="bind port")
    run.add_argument(
        "--reload", action="store_true", help="restart when source files change"
    )
    run.add_argument(
        "--dev",
        action="store_true",
        help="serve an agent class anonymously on localhost, for development only",
    )
    dev = commands.add_parser(
        "dev",
        help="run every agent in agentframe.yaml locally, wired together",
        description=(
            "Run every agent listed in agentframe.yaml on 127.0.0.1 with "
            "generated development API keys and a local registry. "
            "Development only."
        ),
    )
    dev.add_argument("--config", default=PROJECT_FILE, help="the project file to read")
    dev.add_argument(
        "--startup-timeout",
        type=float,
        default=DEFAULT_STARTUP_TIMEOUT_SECONDS,
        help="seconds each agent has to become ready",
    )
    compose = commands.add_parser(
        "compose",
        help="write a development compose.yaml and Dockerfile for agentframe.yaml",
        description=(
            "Write compose.yaml, one container per agent in agentframe.yaml, "
            "and a Dockerfile if there is none. Development only."
        ),
    )
    compose.add_argument(
        "--config", default=PROJECT_FILE, help="the project file to read"
    )
    compose.add_argument(
        "--force", action="store_true", help="overwrite an existing compose.yaml"
    )
    evaluation = commands.add_parser(
        "eval",
        help="run an evaluation and fail when it misses a threshold",
        description=(
            "Run an evaluation, print its summary and exit with 1 when it "
            "misses a threshold or 2 on a configuration error."
        ),
    )
    _add_eval_arguments(evaluation)
    return parser


def _add_eval_arguments(evaluation: argparse.ArgumentParser) -> None:
    evaluation.add_argument("target", help="module:function returning an Evaluation")
    evaluation.add_argument(
        "--json", metavar="PATH", dest="json_path", help="write the JSON report here"
    )
    evaluation.add_argument(
        "--min-pass-rate",
        type=_rate,
        metavar="RATE",
        help="the lowest acceptable pass rate, from 0.0 to 1.0",
    )
    evaluation.add_argument(
        "--max-errors",
        type=_non_negative_integer,
        default=0,
        metavar="N",
        help="the most errored attempts accepted (default: 0)",
    )
    evaluation.add_argument(
        "--max-total-tokens",
        type=_positive_integer,
        metavar="N",
        help="the most tokens the evaluation may use; unknown usage misses it",
    )
    evaluation.add_argument(
        "--allow-not-run",
        action="store_true",
        help="accept attempts that never started",
    )
    evaluation.add_argument(
        "--allow-over-budget",
        action="store_true",
        help="accept usage over the evaluation's own max_total_tokens budget",
    )
    evaluation.add_argument(
        "--capture-content",
        action="store_true",
        help=(
            "allow an evaluation built with capture_content=True; its summary "
            "and JSON report then hold case content"
        ),
    )


def _eval_settings(arguments: argparse.Namespace) -> EvalSettings:
    return EvalSettings(
        target=arguments.target,
        json_path=arguments.json_path,
        capture_content=arguments.capture_content,
        min_pass_rate=arguments.min_pass_rate,
        max_errors=arguments.max_errors,
        max_total_tokens=arguments.max_total_tokens,
        allow_not_run=arguments.allow_not_run,
        allow_over_budget=arguments.allow_over_budget,
    )


def _rate(text: str) -> float:
    try:
        rate = float(text)
    except ValueError:
        rate = math.nan
    if not 0.0 <= rate <= 1.0:
        raise argparse.ArgumentTypeError(f"'{text}' is not a rate from 0.0 to 1.0")
    return rate


def _non_negative_integer(text: str) -> int:
    return _integer(text, minimum=0)


def _positive_integer(text: str) -> int:
    return _integer(text, minimum=1)


def _integer(text: str, *, minimum: int) -> int:
    try:
        number = int(text)
    except ValueError:
        number = minimum - 1
    if number < minimum:
        raise argparse.ArgumentTypeError(
            f"'{text}' is not an integer of {minimum} or more"
        )
    return number


def _compose(project_file: Path, *, force: bool) -> None:
    result = write_compose(project_file, force=force)
    for path in result.written:
        print(f"agentframe compose: wrote {path}")
    if result.kept is not None:
        print(f"agentframe compose: kept the existing {result.kept}")
    print(
        "agentframe compose: development only. Set these in a .env file next "
        "to compose.yaml, each to a different random value: "
        + ", ".join(result.variables)
    )


def serve(settings: RunSettings, *, reload: bool = False) -> None:
    """Build the app described by ``settings`` and serve it until stopped.

    The app is built before the server starts, so configuration errors are
    reported up front, also when reloading. Shutdown is graceful, bounded by
    the app's ``HostConfig.shutdown_grace_seconds``.

    Args:
        settings: What to serve and where.
        reload: Restart the server when source files change.

    Raises:
        CommandError: The target cannot be loaded or is not usable, ``--dev``
            was asked to bind a non-localhost address, Uvicorn is missing, or
            the server failed to start.
        AgentFrameError: The application's configuration is invalid.
    """
    uvicorn = _import_uvicorn()
    _import_from_working_directory()
    app = build_app(settings)
    if not reload:
        _serve_draining(app, settings)
        return
    os.environ[_RELOAD_SETTINGS_VARIABLE] = json.dumps(asdict(settings))
    # noinspection PyProtectedMember
    grace_seconds = app._host.shutdown_grace_seconds
    uvicorn.run(
        f"{__name__}:{_app_for_reload.__name__}",
        factory=True,
        reload=True,
        host=settings.host,
        port=settings.port,
        timeout_graceful_shutdown=math.ceil(grace_seconds),
    )


def _serve_draining(app: AgentApp, settings: RunSettings) -> None:
    """Serve ``app`` until a signal, starting its shutdown on the signal."""
    # Imported here because it needs Uvicorn, an optional dependency.
    from agentframe.hosting.draining_server import DrainingServer

    server = DrainingServer(app, host=settings.host, port=settings.port)
    # Uvicorn re-raises the signal it handled once it has shut down.
    with contextlib.suppress(KeyboardInterrupt):
        server.run()
    if not server.started:
        raise CommandError("The server did not start; see the log above.")


def evaluate(settings: EvalSettings) -> int:
    """Run the evaluation ``settings`` names and check its thresholds.

    Prints the summary (``str(report)``), which holds no case content unless
    the evaluation captured it, and writes the JSON report before checking
    thresholds, so CI keeps it when the check fails. Problems are printed as
    one ``agentframe: error: ...`` line.

    Args:
        settings: The evaluation, report path and thresholds.

    Returns:
        0 when every threshold is met; 1 when one is missed, after printing
        each miss above the summary; 2 when the target cannot be loaded, is not
        an ``Evaluation``, captures content without ``capture_content``, its
        factory raises an AgentFrame error, or the report cannot be written.
    """
    try:
        evaluation = load_evaluation(settings)
        report = asyncio.run(evaluation.run())
        if settings.json_path is not None:
            _write_report(report, settings.json_path)
    except (CommandError, AgentFrameError) as error:
        print(f"agentframe: error: {error}", file=sys.stderr)
        return 2
    try:
        report.require(
            min_pass_rate=settings.min_pass_rate,
            max_errors=settings.max_errors,
            max_total_tokens=settings.max_total_tokens,
            allow_not_run=settings.allow_not_run,
            allow_over_budget=settings.allow_over_budget,
        )
    except AssertionError as miss:
        # The message lists each miss above the same summary.
        print(miss, flush=True)
        print("agentframe: error: The evaluation missed a threshold.", file=sys.stderr)
        return 1
    print(report)
    return 0


def load_evaluation(settings: EvalSettings) -> Evaluation:
    """Build the ``Evaluation`` that ``settings`` names, without running it.

    Args:
        settings: The evaluation's target and whether content may be captured.

    Returns:
        The evaluation the application's factory returned.

    Raises:
        CommandError: The target cannot be loaded, is not a function returning
            an ``Evaluation``, or captures content without
            ``capture_content``.
        AgentFrameError: The application's evaluation is invalid.
    """
    _import_from_working_directory()
    factory = _load(settings.target)
    if not callable(factory):
        raise CommandError(
            f"'{settings.target}' is not a function that returns an Evaluation."
        )
    evaluation = factory()
    if not isinstance(evaluation, Evaluation):
        raise CommandError(
            f"'{settings.target}' returned {type(evaluation).__name__}, "
            "not an Evaluation."
        )
    if evaluation.capture_content and not settings.capture_content:
        raise CommandError(
            f"'{settings.target}' captures content (capture_content=True), so "
            "its summary and report would hold case inputs and outputs; pass "
            "--capture-content to allow that."
        )
    return evaluation


def _write_report(report: EvalReport, path: str) -> None:
    try:
        report.write_json(path)
    except OSError as error:
        raise CommandError(
            f"Cannot write the JSON report to '{path}': {error.strerror}."
        ) from error


def _import_from_working_directory() -> None:
    # A console script's import path starts at the script's directory, not the
    # working directory the application's modules live in.
    if os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())


def build_app(settings: RunSettings) -> AgentApp:
    """Build the ``AgentApp`` that ``settings`` describes, without serving it.

    Args:
        settings: What to serve and where.

    Returns:
        The application's own app, or a development app with ``dev``.

    Raises:
        CommandError: The target cannot be loaded or is not usable, or
            ``--dev`` was asked to bind a non-localhost address.
        AgentFrameError: The application's configuration is invalid.
    """
    target = _load(settings.target)
    if settings.dev:
        return _development_app(target, settings)
    return _app_from_factory(target, settings.target)


def _app_for_reload() -> AgentApp:
    """Rebuild the app in Uvicorn's reload worker from the parent's settings."""
    _configure_logging()
    settings = RunSettings(**json.loads(os.environ[_RELOAD_SETTINGS_VARIABLE]))
    return build_app(settings)


def _configure_logging() -> None:
    # Match Uvicorn's own log lines so AgentFrame's warnings read the same way.
    logging.basicConfig(format="%(levelname)s:     %(message)s")
    # Under agentframe dev, show each run's start and end with its
    # invocation IDs, so the combined log can be followed across agents.
    if os.environ.get(DEV_VARIABLE) == "1":
        logger.setLevel(logging.INFO)


def _load(target: str) -> object:
    module_name, separator, attribute = target.partition(":")
    if not separator or not module_name or not attribute:
        raise CommandError(
            f"'{target}' is not an import path; use module:name, for example "
            "myapp.hosting:create_app."
        )
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        # Only the target module itself is "not found"; a missing dependency
        # inside it is the application's bug and keeps its traceback.
        if error.name != module_name and not module_name.startswith(f"{error.name}."):
            raise
        raise CommandError(f"Cannot import module '{module_name}'.") from error
    try:
        return getattr(module, attribute)
    except AttributeError:
        raise CommandError(
            f"Module '{module_name}' has no attribute '{attribute}'."
        ) from None


def _app_from_factory(factory: object, target: str) -> AgentApp:
    if not callable(factory):
        raise CommandError(f"'{target}' is not a function that returns an AgentApp.")
    app = factory()
    if isinstance(app, AgentApp):
        return app
    hint = (
        " To serve an agent class for local development, add --dev."
        if isinstance(factory, type) and issubclass(factory, Agent)
        else ""
    )
    raise CommandError(
        f"'{target}' returned {type(app).__name__}, not an AgentApp.{hint}"
    )


def _development_app(agent_class: object, settings: RunSettings) -> AgentApp:
    if not (isinstance(agent_class, type) and issubclass(agent_class, Agent)):
        raise CommandError(f"--dev needs an Agent class; '{settings.target}' is not.")
    if not is_loopback_host(settings.host):
        raise CommandError(
            f"--dev serves anonymously, so it only binds to localhost, not "
            f"'{settings.host}'. Use a create_app() factory with real "
            "authentication for other hosts."
        )
    try:
        agent = agent_class()
    except TypeError as error:
        raise CommandError(
            f"--dev cannot construct {agent_class.__name__}() without arguments "
            f"({error}). Use a create_app() factory instead."
        ) from error
    app = AgentApp(
        agent=agent,
        host=HostConfig(
            public_url=f"http://{_url_host(settings.host)}:{settings.port}",
            auth=AllowAnonymous(),
        ),
    )
    logger.warning(
        "Development mode: serving %s with AllowAnonymous, so any local process "
        "can call it without credentials. Never use --dev in production.",
        agent_class.__name__,
    )
    return app


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def _import_uvicorn() -> ModuleType:
    try:
        import uvicorn
    except ImportError:
        raise CommandError(
            "agentframe run needs the uvicorn ASGI server; install it with "
            "pip install 'agentframe-py[server]'."
        ) from None
    return uvicorn
