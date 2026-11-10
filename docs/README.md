# AgentFrame design documentation

These documents describe a proposed design, not an implemented or stable API.

| Document | Purpose |
| --- | --- |
| [Initial outline](initial-outline.md) | Product boundaries, architectural rules, defaults, and independently reviewable delivery gates. |
| [Annotations](annotations.md) | Agent, skill, and tool decorators; Python data contracts; proposed usage. |
| [Base classes](base-classes.md) | Complete annotation/subclass/composition decision table, interfaces, and small proposed API examples. |
| [Agent examples](agent-examples.md) | Model-free, model-backed, and mixed multi-skill agents, model selection, local runtime invocation, and A2A boundaries. |
| [Limits](limits.md) | Per-request output/input limits, invocation budgets, host ceilings, and honest accounting guarantees. |
| [Tracing](tracing.md) | Optional OpenTelemetry configuration, span boundaries, safe metadata, and correlation. |
| [MCP skills](mcp-skills.md) | Optional allowlisted skill publication, shared A2A/MCP hosting, schema mapping, and protocol boundaries. |
| [Authorization](authorization.md) | Skill permission requirements, verified principals, application role/resource policy, and fail-closed execution. |
| [Prompt safeguarding](prompt-safeguarding.md) | Shared responsibilities, content-policy hooks, data egress, and prompt-injection limitations. |
| [Repository context](../AGENTS.md) | Durable contributor and coding-agent instructions, including framework independence and scope boundaries. |

Read the outline first. Changes to proposed interfaces should keep the design
documents consistent. Implementation must select and pin a compatible A2A SDK
release and protocol target; references to the latest specification are not a
promise of automatic compatibility with future releases.
