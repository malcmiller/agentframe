# AgentFrame

**Define agents with Python. Communicate through A2A. Choose your model host.**

AgentFrame is a proposed Python library for creating agents through classes,
decorators, and type annotations. Agent-to-Agent (A2A) interoperability and
Agent Cards are core contracts, not optional exports.

AgentFrame is an independent agent framework, not a wrapper around LangChain,
LangGraph, Microsoft Agent Framework, Pydantic AI, Google ADK, CrewAI, or another
major agent package. It owns its authoring API and execution behavior while
reusing protocol SDKs, validation libraries, and model-provider clients.

## Status

This repository currently contains initial design documentation only. There is
no implemented SDK, installable release, or supported public API yet. All names,
interfaces, examples, and defaults in the documentation are proposals.

## Goals

- Make a small agent easy to define with explicit Python declarations.
- Generate native A2A Agent Cards from agent and skill metadata plus host configuration.
- Support model-free agents and optional model-assisted behavior.
- Start with Microsoft Foundry model inference and local Llama through Ollama.
- Allow model selection to change without rewriting agent behavior.
- Provide safe, validated defaults with typed configuration and dependency injection.
- Build on the official A2A Python SDK instead of maintaining another wire format.

## Proposed authoring experience

This is design syntax, not an implemented API. Decorators describe the agent;
an `Agent` subclass implements behavior. Models and infrastructure are injected.
Each ability is a separate `@skill` method; no abstract `handle()` or manual
dispatch method is required.

```python
from agentframe import Agent, RunContext, agent, skill


@agent(name="Echo", description="Returns supplied text.", version="0.1.0")
class Echo(Agent):
    @skill(id="echo", name="Echo text", description="Return text.", tags=["text"])
    async def echo(self, text: str, ctx: RunContext) -> str:
        return text
```

The normal path needs only one AgentFrame subclass, `Agent`. Model-free agents
need no provider configuration. See [agents with and without models](docs/agent-examples.md)
for multi-skill, mixed, and local invocation examples, and the
[API design examples](docs/base-classes.md#proposed-api-examples) for host
composition and advanced extension points.

Llama is a model family; Ollama is the initial local model host. Foundry model
inference is distinct from deploying an agent to Foundry Agent Service.

## First success criterion

Define one text agent in approximately 25 lines of agent-authoring code, generate
and publish a valid Agent Card, invoke it with an independent official A2A client,
and switch between a Foundry GPT deployment and local Llama by changing only model
configuration. Host bootstrap and credential setup are documented separately.

The same public contract must work with both hosts; identical generated answers
are not required. A deterministic model-free example must work without cloud
credentials, model downloads, or an inference server.

## Initial scope

The first release focuses on agent authoring, native A2A hosting, model adapters,
validation, cancellation, and bounded execution. It does not include a UI,
multi-agent orchestration platform, durable workflow engine, federation, RAG
system, evaluation platform, or Foundry-hosted agent deployment.

Token budgets and deadlines are runtime-enforced configuration; tracing is an
optional OpenTelemetry integration with content capture off by default. MCP is
a planned optional adapter for explicitly allowlisted skills, not automatic
publication of every skill or internal tool. A custom REST skill API is deferred.

Skills may declare required permissions; application roles map to grants outside
the agent definition. The proposed runtime enforces verified identity and
application resource policy across entry points. Prompt safeguarding combines
library trust boundaries with application checks, not a guaranteed injection filter.

AgentFrame may become a foundation for a larger agentic platform. That platform
is not a prerequisite for the first useful library.

## Documentation

- [Documentation index](docs/README.md)
- [Initial outline and delivery gates](docs/initial-outline.md)
- [Decorators and type annotations](docs/annotations.md)
- [Base classes and interfaces](docs/base-classes.md)
- [Agent examples: model-free, model-backed, and mixed](docs/agent-examples.md)
- [Execution limits and token accounting](docs/limits.md)
- [Tracing and correlation](docs/tracing.md)
- [A2A hosting and optional MCP skill configuration](docs/mcp-skills.md)
- [Skill authorization and caller identity](docs/authorization.md)
- [Prompt safeguarding and shared responsibilities](docs/prompt-safeguarding.md)
- [Contributor and coding-agent context](AGENTS.md)

## Development and contributions

The project is in API-design discovery. Proposals should connect to the first
success criterion and identify interoperability, safety, and authoring tradeoffs.
There are no build or test commands until the Python package is introduced.

The intended repository and distribution name is `agentframe`; the intended
Python import is `agentframe`. Package-index names are not reserved by creating
this repository, and no package availability or trademark clearance is guaranteed.

## References

- [A2A specification](https://a2a-protocol.org/latest/specification/)
- [Official A2A Python SDK](https://github.com/a2aproject/a2a-python)
- [Ollama Python client](https://github.com/ollama/ollama-python)
- [Microsoft Foundry documentation](https://learn.microsoft.com/en-us/azure/ai-foundry/)

## License

Licensed under the [Apache License 2.0](LICENSE), a permissive open-source license
that includes an explicit patent grant subject to its terms.
