# Contributing to AgentFrame

Thanks for helping. This page covers setup, the checks every change must pass
and how pull requests work. [AGENTS.md](AGENTS.md) holds the full contributor
and coding-agent rules, including the design decisions and scope boundaries.

## Readability first

Human readability is the top-priority contribution rule: prefer simple,
maintainable code over cleverness or speculative abstractions, keep functions
small and diffs focused, and fully type and document public APIs. See
[code quality and readability](AGENTS.md#code-quality-and-readability).
Proposals should explain their interoperability, safety and authoring
tradeoffs.

## Setup

Use uv 0.13.1 (for example `brew install uv` on macOS; check the installed
version). From a fresh clone, one command installs the package, its extras and
the development tools, downloading Python if needed (the default interpreter
is Python 3.13):

```bash
uv sync
```

Development commands:

```bash
uv run ruff format
uv run ruff check --fix
uv run mypy
uv run pytest
uv run pre-commit install
uv run pre-commit run --all-files
uv build
```

## Checks

All four checks must pass before committing:

```bash
uv run ruff format --check
uv run ruff check
uv run mypy
uv run pytest
```

CI runs them on Python 3.13, 3.14 and 3.15, verifies an isolated wheel import,
runs the [examples smoke runner](README.md#examples) against the installed
wheel, and runs the tests on the lowest allowed dependency versions
(`lowest-deps`). Pre-commit runs Ruff and mypy; tests run in CI, not on every
commit. Run the matrix locally with `uv run --python 3.13 pytest` (repeat for
3.14 and 3.15).

Measure coverage, including the agents and CLI that tests start as
subprocesses, with `uv run coverage run -m pytest`, then
`uv run coverage combine` and `uv run coverage report` (CI does the same).

Tests against a real Ollama server or Foundry deployment are opt-in and never
run in normal CI; see [real-provider tests](docs/guides/provider-tests.md).

## Pull requests

- Open a pull request into `main`; nobody pushes to `main` directly.
- CI must pass: the checks on Python 3.13, 3.14 and 3.15, `build` and
  `lowest-deps`.
- Add an entry under `Unreleased` in [CHANGELOG.md](CHANGELOG.md) for every
  user-visible change.
- Keep documentation consistent with the change (see
  [documentation maintenance](AGENTS.md#documentation-maintenance)).

## Dependencies and workflows

Runtime dependencies and extras are compatible ranges; development tools and
hatchling are pinned exactly in `pyproject.toml`, and `uv.lock` is committed.
Every workflow action is pinned to a full commit SHA with its release tag in a
comment, and Dependabot proposes weekly updates to the actions, development
tools and `uv.lock`. See
[resolved platform decisions](docs/roadmap.md#resolved-m0-platform-and-api-decisions).

## Releasing

Releases are published to PyPI by a workflow; maintainers follow
[releasing](docs/guides/releasing.md).

## Security

Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md),
never in a public issue or pull request.
