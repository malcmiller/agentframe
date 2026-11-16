# Stability, deprecation and upgrading

AgentFrame is before 1.0, so its public API can still change. This page says
what can change, how changes are announced and what 1.0 will require.

## What is public

The public API is:

- every name importable from `agentframe` and from `agentframe.evaluation`,
  as documented in the [API reference](../api/README.md);
- the `agentframe` commands and their options (`run`, `dev`, `compose`,
  `eval`) and the `agentframe.yaml` format;
- the wire formats other programs depend on: the
  [skill-call extension](../api/hosting.md#structured-skill-call-wire-format)
  (its URI ends in `/v1`) and the evaluation JSON report (its `schema` is
  `agentframe.evaluation.report/1`).

Not public: modules imported by their full internal path (for example
`agentframe.runtime.limits`), names starting with `_`, log message text, and
anything the reference marks **proposed** or **open** rather than implemented.

## Before 1.0

- A **minor** release (for example 0.3.0 to 0.4.0) may change or remove
  public API.
- A **patch** release (for example 0.3.0 to 0.3.1) only fixes bugs,
  documentation or security issues and does not break the public API.
- A wire format changes only with a new version in its identifier (a new
  extension URI or report `schema`), never in place.

To take fixes but not breaking changes, depend on the next minor as the upper
bound, for example `agentframe-py>=0.3.0,<0.4`.

## Deprecations

When a change can keep the old behavior working for a while, it does:

- the old name or behavior keeps working for **at least one minor release**
  and raises a `DeprecationWarning` that names its replacement and the release
  that removes it;
- the [changelog](../../CHANGELOG.md) lists it under **Deprecated**, and later
  its removal under **Removed**;
- the reference entry says it is deprecated and links the replacement.

Some changes cannot keep the old behavior, for example a security fix or a
protocol change. Those break in one release and say so in **Upgrading**.

Run your tests with warnings shown (pytest shows them by default) or as errors
(`-W error::DeprecationWarning`) to find deprecated use before it is removed.

## Upgrading notes

From the release after 0.3.0, every release that changes public API or
behavior starts its [changelog](../../CHANGELOG.md) entry with an
**Upgrading** section: each change you may need to make, with the old and new
code where it helps. Read it before upgrading; a later release without one
needs no code changes. Earlier releases list their changes without this
section.

## Criteria for 1.0

1.0 is declared when:

- every public name has an implemented reference entry with no open
  signature, default or error questions (see
  [decisions still needed](../api/README.md#decisions-still-needed));
- the open contracts in the [roadmap](../roadmap.md) that would change those
  signatures are decided and shipped, or explicitly left out of 1.0;
- at least one minor release has shipped with no breaking change to that
  surface;
- the supported Python versions, A2A protocol version and dependency ranges
  are tested in CI, including the lowest allowed versions.

From 1.0, AgentFrame follows [Semantic Versioning](https://semver.org/):
breaking changes only in a major release, after at least one minor release of
deprecation where possible.
