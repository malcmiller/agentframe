# AgentFrame

**Define agents with Python. Communicate through A2A. Choose your model host.**

[![PyPI](https://img.shields.io/pypi/v/agentframe-py)](https://pypi.org/project/agentframe-py/)
[![Python versions](https://img.shields.io/pypi/pyversions/agentframe-py)](https://pypi.org/project/agentframe-py/)
[![CI](https://github.com/malcmiller/agentframe/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/malcmiller/agentframe/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/malcmiller/agentframe)](https://github.com/malcmiller/agentframe/blob/main/LICENSE)

AgentFrame is a Python library for building agents with classes, decorators and
type annotations. Every agent gets an Agent Card and an A2A 1.0 endpoint, so it
can work with other A2A agents, including ones built with other frameworks. Run
a model locally with Ollama or in Microsoft Foundry by changing configuration,
not code. Build one agent or many.

AgentFrame is an independent framework, not a wrapper around LangChain,
Microsoft Agent Framework or similar packages.

## Features

- **Authoring:** `@agent`, `@skill` and `Inject[T]`, with inputs and outputs
  validated by Python types.
- **A2A 1.0 hosting:** an Agent Card and JSON-RPC endpoint per agent, served
  with `agentframe run`.
- **Models:** local Llama through Ollama or Microsoft Foundry GPT deployments.
- **Structured output** as validated Pydantic instances.
- **Tool calling** in a bounded, authorized loop.
- **Model routing:** opt-in routing of free-text requests to skills.
- **Multi-agent composition:** `Remote` calls carrying deadlines, budgets and
  identity; `agentframe dev` and `agentframe compose`.
- **Model-planned orchestration** with remote skills as tools or checked plans.
- **Evaluation** of local skills and A2A agents, gating CI.
- **Security and limits:** API keys and Entra ID, permissions, execution
  policies, run and host limits.

## Installation

AgentFrame needs Python 3.13 or later and is imported as `agentframe`:

```shell
pip install "agentframe-py[server,ollama]"
```

The extras add what a deployment needs:

| Extra | Adds |
| --- | --- |
| `server` | Uvicorn, for the `agentframe run` and `agentframe dev` commands. |
| `ollama` | `OllamaModel`, for local Llama through Ollama. |
| `foundry` | `FoundryModel`, `FoundryAgentRegistry` and `FoundryA2AToolRegistry`, for Microsoft Foundry. |
| `entra` | `EntraIDAuth` and `OnBehalfOf`, for Microsoft Entra ID. |

Pre-1.0: public APIs may change in minor releases; changes are listed in the
[changelog](https://github.com/malcmiller/agentframe/blob/main/CHANGELOG.md).
See the
[stability, deprecation and upgrade policy](https://github.com/malcmiller/agentframe/blob/main/docs/guides/stability.md).

## Quick start

Decorators describe the agent; an `Agent` subclass implements behavior, one
`@skill` method per ability. This model-free agent is
[`examples/echo.py`](https://github.com/malcmiller/agentframe/blob/main/examples/echo.py);
run the `await` lines inside an async function or `asyncio.run`:

```python
from agentframe import Agent, Inject, RunContext, Runtime, Services, agent, skill


class Greeter:
    """A service the ``greet`` skill receives by injection."""

    greeting = "Hello"

    def greet(self, name: str) -> str:
        return f"{self.greeting}, {name}!"


@agent(version="0.1.0")
class Echo(Agent):
    """Returns supplied text or a greeting."""

    @skill(tags=["text"])
    async def echo(self, text: str, ctx: RunContext) -> str:
        """Return the text unchanged."""
        return text

    @skill(tags=["greeting"])
    async def greet(self, name: str, greeter: Inject[Greeter], ctx: RunContext) -> str:
        """Greet someone by name."""
        return greeter.greet(name)


runtime = Runtime(services=Services().add(Greeter, Greeter()))

value = await runtime.invoke(Echo(), skill="echo", arguments={"text": "hi"})
result = await runtime.run(Echo(), skill="greet", arguments={"name": "Ada"})
```

Agent names default to the class name, skill IDs and names to the method name,
and descriptions to docstrings. The
example's `create_app()` serves `Echo` to any A2A 1.0 client:

```shell
ECHO_API_KEY=dev-only-key agentframe run examples.echo:create_app
```

The card is at `/.well-known/agent-card.json` and the endpoint at `/a2a`. The
`X-API-Key` key is a **development placeholder**; production needs real
authentication, such as
[`EntraIDAuth`](https://github.com/malcmiller/agentframe/blob/main/docs/api/authorization.md#entraidauth).

## Using a model

[`examples/assistant.py`](https://github.com/malcmiller/agentframe/blob/main/examples/assistant.py)
never names a model host. Its text skill is all a text agent needs:

```python
@agent(version="1.0.0")
class Assistant(Agent):
    """Answers questions, summarizes text and counts words."""

    instructions = "Answer in one short sentence."
    model_settings = ModelSettings(max_output_tokens=512)
    # Foundry does not report a deployment's context window, so cap the input.
    limits = RunLimits(max_input_tokens=4000)

    @skill(tags=["questions"])
    async def answer(self, question: str, ctx: RunContext) -> str:
        """Answer a question briefly."""
        return await ctx.generate(question)
```

Its `create_app()` gets the model from
[`model_from_env()`](https://github.com/malcmiller/agentframe/blob/main/docs/api/models.md#model_from_env),
which reads AgentFrame's model variables: an installed Ollama model tag
(nothing is downloaded), or a Foundry GPT deployment signed in with
`DefaultAzureCredential` (`az login` locally, managed identity in Azure; no API
key). Switching providers changes only the environment:

```python
from agentframe import AgentApp, model_from_env

app = AgentApp(agent=Assistant(model=model_from_env()), host=host)
```

Run it with either provider:

```shell
AGENTFRAME_MODEL_PROVIDER=ollama AGENTFRAME_OLLAMA_MODEL=llama3.2:3b \
    agentframe run examples.assistant:create_app

AGENTFRAME_MODEL_PROVIDER=foundry \
    AGENTFRAME_FOUNDRY_ENDPOINT=https://<resource>.openai.azure.com/openai/v1/ \
    AGENTFRAME_FOUNDRY_DEPLOYMENT=<deployment> \
    agentframe run examples.assistant:create_app
```

## Multi-agent

An orchestrator is an ordinary agent whose skills call other agents over A2A
through `Remote[Literal["name"]]` parameters: `call` runs a named skill with a
validated result, `ask` sends free text to any A2A agent.

```python
import asyncio
from typing import Literal

from agentframe import Agent, Remote, RunContext, agent, skill

type Research = Remote[Literal["research-agent"]]


@agent(name="Planner", version="0.1.0")
class Planner(Agent):
    """Plans a goal with help from a research agent."""

    @skill(tags=["planning"])
    async def plan(
        self, goal: str, topics: list[str], research: Research, ctx: RunContext
    ) -> list[str]:
        """Brief every topic of a goal at once."""
        async with asyncio.TaskGroup() as group:
            briefs = [
                group.create_task(
                    research.ask(f"Brief me on {topic} for {goal}", max_total_tokens=2_000)
                )
                for topic in topics
            ]
        return [brief.result() for brief in briefs]
```

The planner finds `"research-agent"` by name in its `AgentDirectory`, or through
`agentframe dev` and an `agentframe.yaml`, as in
[`examples/multi_agent`](https://github.com/malcmiller/agentframe/tree/main/examples/multi_agent).
Each call carries the remaining deadline, its token ceiling, the hop count and
the caller's identity. To let the model choose which agents to call, read the
[orchestration guide](https://github.com/malcmiller/agentframe/blob/main/docs/guides/orchestration.md).

## Examples

Each example has a `create_app()` you can serve with
`agentframe run examples.<name>:create_app`. Model-backed examples choose
Ollama or Foundry with `model_from_env()`, as in
[using a model](https://github.com/malcmiller/agentframe/blob/main/README.md#using-a-model);
`examples/multi_agent` uses local Llama only, through `AGENTFRAME_OLLAMA_MODEL`.

| Example | Shows |
| --- | --- |
| [`examples/echo.py`](https://github.com/malcmiller/agentframe/blob/main/examples/echo.py) | A model-free agent with an injected service and a development API key. |
| [`examples/assistant.py`](https://github.com/malcmiller/agentframe/blob/main/examples/assistant.py) | Text generation, a typed structured output and a model-free skill on one agent. |
| [`examples/orders.py`](https://github.com/malcmiller/agentframe/blob/main/examples/orders.py) | Tool calling with permissions and a tool policy. |
| [`examples/routed_assistant.py`](https://github.com/malcmiller/agentframe/blob/main/examples/routed_assistant.py) | Free text routed to a skill by the built-in `ModelRouter`. |
| [`examples/multi_agent`](https://github.com/malcmiller/agentframe/tree/main/examples/multi_agent) | A planner calling research and writer agents, by hand and by model-made plan; run with `agentframe dev`. |
| [`examples/plan_approval.py`](https://github.com/malcmiller/agentframe/blob/main/examples/plan_approval.py) | Human approval of model-made plans before they run. |

`uv run python -m examples.smoke` checks every example with a scripted model.

## Documentation

- [Documentation index](https://github.com/malcmiller/agentframe/blob/main/docs/README.md)
- [API reference](https://github.com/malcmiller/agentframe/blob/main/docs/api/README.md)
- [Agent examples: model-free, model-backed and mixed](https://github.com/malcmiller/agentframe/blob/main/docs/guides/agent-examples.md)
- [Local development and containers](https://github.com/malcmiller/agentframe/blob/main/docs/guides/development.md)
- [Authorization and caller identity](https://github.com/malcmiller/agentframe/blob/main/docs/guides/authorization.md)
- [Orchestration](https://github.com/malcmiller/agentframe/blob/main/docs/guides/orchestration.md)
- [Evaluating agents](https://github.com/malcmiller/agentframe/blob/main/docs/guides/evaluation.md)
- [Roadmap](https://github.com/malcmiller/agentframe/blob/main/docs/roadmap.md)
- [Changelog](https://github.com/malcmiller/agentframe/blob/main/CHANGELOG.md)

## Contributing, security and license

Contributions are welcome; see
[CONTRIBUTING.md](https://github.com/malcmiller/agentframe/blob/main/CONTRIBUTING.md).
Report vulnerabilities privately as described in
[SECURITY.md](https://github.com/malcmiller/agentframe/blob/main/SECURITY.md).
Licensed under the
[Apache License 2.0](https://github.com/malcmiller/agentframe/blob/main/LICENSE).
