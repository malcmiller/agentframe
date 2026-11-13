# Releasing

AgentFrame is published to PyPI by
[`.github/workflows/release.yml`](../../.github/workflows/release.yml) when the
maintainer publishes a GitHub release. Nobody uploads from a laptop, and no API
token is stored: both indexes trust the workflow through
[trusted publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/).

The PyPI distribution is named `agentframe-py`, because PyPI refuses
`agentframe` as too similar to an existing project. The import package and the
`agentframe` command keep their names.

## What the workflow does

Publishing a GitHub release for a tag such as `v0.1.0` starts the workflow:

1. **Check:** the tag must point at a commit on `main`; format, lint, type
   checks and tests run on Python 3.13.
2. **Build:** the tag must equal `v` plus the `pyproject.toml` version, and that
   version must not already exist on PyPI or TestPyPI. The wheel and sdist are
   built once and pass the same installed-wheel checks as CI, including the
   [examples smoke runner](../../README.md#runnable-examples).
3. **TestPyPI:** the same files are uploaded to TestPyPI (environment
   `testpypi`), then installed in a clean Python 3.13 environment, where the
   examples smoke runner runs against them. Only `agentframe-py` itself comes
   from TestPyPI; its dependencies come from PyPI.
4. **PyPI:** after a reviewer approves the `pypi` environment, the same files
   are uploaded to PyPI. Each upload confirms the files' SHA-256 checksums
   first and publishes signed [PEP 740](https://peps.python.org/pep-0740/)
   attestations.
5. **Verify:** `pip install agentframe-py` from PyPI runs the examples smoke runner
   on Python 3.13, 3.14 and 3.15.

A version can be uploaded to an index only once. If a later job fails for a
transient reason, use **Re-run failed jobs**; re-running all jobs stops at the
build because the version already exists. A real problem needs a new version.

## One-time setup

Do these once, before the first release.

1. **Register a pending trusted publisher on PyPI.** Signed in to
   [pypi.org](https://pypi.org), open
   [Publishing](https://pypi.org/manage/account/publishing/) and add a GitHub
   publisher:

   | Field | Value |
   | --- | --- |
   | PyPI project name | `agentframe-py` |
   | Owner | `malcmiller` |
   | Repository name | `agentframe` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

2. **Register a separate pending trusted publisher on TestPyPI.** TestPyPI is a
   separate service with its own account. On
   [test.pypi.org Publishing](https://test.pypi.org/manage/account/publishing/),
   add the same values (project name `agentframe-py`) with environment name
   `testpypi`.

3. **Create the GitHub environments.** In the repository's **Settings →
   Environments**:
   - `testpypi`: under **Deployment branches and tags**, choose **Selected
     branches and tags** and add the tag rule `v*`.
   - `pypi`: enable **Required reviewers** (add yourself) and add the same `v*`
     tag rule.

## Publish a release

1. Merge a pull request to `main` that sets the new version in
   `pyproject.toml` and `agentframe.__version__`, and adds its
   [changelog](../../CHANGELOG.md) entry, with the release date in its heading
   (`## [0.1.0] - YYYY-MM-DD`); set the date before tagging. Wait for CI to
   pass on `main`.
2. Create a GitHub release: **Releases → Draft a new release**, create the tag
   (for example `v0.1.0`) on `main`, title it with the version, and link the
   changelog entry in the notes, for example
   `See the [changelog](https://github.com/malcmiller/agentframe/blob/main/CHANGELOG.md).`
3. Publish the release. Follow the **Release** workflow run in **Actions**.
4. When the TestPyPI checks pass, approve the `pypi` deployment.
5. Confirm `pip install agentframe-py` installs the new version.
