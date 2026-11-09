# AgentFrame design documentation

These documents describe a proposed design, not an implemented or stable API.

| Document | Purpose |
| --- | --- |
| [Initial outline](initial-outline.md) | Product boundaries, architectural rules, defaults, and independently reviewable delivery gates. |
| [Annotations](annotations.md) | Agent, skill, and tool decorators; Python data contracts; proposed usage. |
| [Base classes](base-classes.md) | Agent behavior, model adapters, execution context, configuration, and A2A integration. |

Read the outline first. Changes to proposed interfaces should keep all three
documents consistent. Implementation must select and pin a compatible A2A SDK
release and protocol target; references to the latest specification are not a
promise of automatic compatibility with future releases.
