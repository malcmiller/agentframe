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
Microsoft Agent Framework or similar packages: it owns its authoring API and
execution behavior, and reuses the official A2A SDK, Pydantic and model-provider
clients.

## Features

- **Authoring:** describe agents with `@agent` and `@skill`, implement each
  skill as an async method, validate inputs and outputs with Python types, and
  inject services with `Inject[T]`.
- **A2A 1.0 hosting:** `AgentApp` serves an Agent Card and a JSON-RPC endpoint
  that any A2A client can call; `agentframe run` serves it with one command.
- **Models:** local Llama through Ollama or GPT deployments in Microsoft
  Foundry, behind one `ctx.generate` call.
- **Structured output:** `ctx.generate(text, output=Summary)` returns a
  validated Pydantic instance.
- **Tool calling:** registered `@tool` functions run in a bounded loop, each
  call authorized before it runs.
- **Model routing:** opt-in `ModelRouter` sends free-text requests to the right
  skill, or asks the caller a question.
- **Multi-agent composition:** call other A2A agents with `Remote` bindings,
  with deadlines, token budgets, hop limits and identity carried across calls;
  run many agents locally with `agentframe dev` or in containers with
  `agentframe compose`.
- **Model-planned orchestration:** let a model call other agents' skills as
  tools, or make and run a checked plan.
- **Evaluation:** score local skills and A2A agents against typed cases and
  gate CI on the report.
- **Security and limits:** authentication (API keys, Microsoft Entra ID),
  per-skill permissions, execution policies, token, call and time limits, and
  host request limits.

## Installation

AgentFrame needs Python 3.13 or later:

```shell
pip install agentframe-py
```

The distribution is named `agentframe-py`, but you import it as `agentframe`.
Optional extras add what a deployment needs:

| Extra | Adds |
| --- | --- |
| `server` | Uvicorn, for the `agentframe run` and `agentframe dev` commands. |
| `ollama` | `OllamaModel`, for local Llama through Ollama. |
| `foundry` | `FoundryModel`, for Microsoft Foundry GPT deployments, and `FoundryAgentRegistry`, for Foundry agents. |
| `entra` | `EntraIDAuth`, for Microsoft Entra ID caller authentication, and `OnBehalfOf` remote calls. |

For example, `pip install "agentframe-py[server,ollama]"`.

Pre-1.0: public APIs may change in minor releases; changes are listed in the
[changelog](https://github.com/malcmiller/agentframe/blob/main/CHANGELOG.md).

## Quick start

Decorators describe the agent; an `Agent` subclass implements behavior, one
`@skill` method per ability. This model-free agent is
[`examples/echo.py`](https://github.com/malcmiller/agentframe/blob/main/examples/echo.py);
run the `await` lines inside an async function or `asyncio.run`:

```python
from agentframe import Agent, Inject, RunContext, Runtime, Services, agent, skill


class Greeter:
    greeting = "Hello"

    def greet(self, name: str) -> str:
        return f"{self.greeting}, {name}!"


@agent(version="0.1.0")
class Echo(Agent):
    """Returns supplied text or a greeting."""

    @skill(id="echo", tags=["text"])
    async def echo(self, text: str, ctx: RunContext) -> str:
        """Return the text unchanged."""
        return text

    @skill(id="greet", tags=["greeting"])
    async def greet(self, name: str, greeter: Inject[Greeter], ctx: RunContext) -> str:
        """Greet someone by name."""
        return greeter.greet(name)


services = Services()
services.add(Greeter, Greeter())
runtime = Runtime(services=services)

value = await runtime.invoke(Echo(), skill="echo", arguments={"text": "hi"})
result = await runtime.run(Echo(), skill="greet", arguments={"name": "Ada"})
```

Names default to the class and method names, descriptions to docstrings.

To serve it to any A2A 1.0 client, the example's `create_app()` wraps `Echo` in
an `AgentApp` with a development API key. Run it with the `server` extra:

```shell
ECHO_API_KEY=dev-only-key agentframe run examples.echo:create_app
```

The Agent Card is at `/.well-known/agent-card.json` and the A2A endpoint at
`/a2a`; callers send the key in the `X-API-Key` header. The key is a
**development placeholder**: production hosts need real authentication, such as
[`EntraIDAuth`](https://github.com/malcmiller/agentframe/blob/main/docs/api/authorization.md#entraidauth).

## Using a model

[`examples/assistant.py`](https://github.com/malcmiller/agentframe/blob/main/examples/assistant.py)
never names a model host. Its text skill is all a text agent needs:

```python
@agent(version="1.0.0")
class Assistant(Agent):
    """Answers questions, summarizes text and counts words."""

    instructions = "Answer in one short sentence."

    @skill("answer", tags=["questions"])
    async def answer(self, question: str, ctx: RunContext) -> str:
        """Answer a question briefly."""
        return await ctx.generate(question)
```

Its `create_app()` builds the model from environment variables. Serve it with
local Llama through Ollama (the model tag must already be installed; nothing is
downloaded):

```shell
AGENTFRAME_MODEL_PROVIDER=ollama AGENTFRAME_OLLAMA_MODEL=llama3.2:3b \
    agentframe run examples.assistant:create_app
```

or with a Foundry GPT deployment, signed in through the Azure CLI (`az login`;
no API key):

```shell
AGENTFRAME_MODEL_PROVIDER=foundry \
    AGENTFRAME_FOUNDRY_ENDPOINT=https://<resource>.openai.azure.com/openai/v1/ \
    AGENTFRAME_FOUNDRY_DEPLOYMENT=<deployment> \
    agentframe run examples.assistant:create_app
```

The example also has a `summarize` skill returning a typed Pydantic result and
a model-free `count_words` skill.

## Multi-agent

An orchestrator is an ordinary agent whose skill calls other agents over A2A.
It names each one with a `Remote[Literal["name"]]` parameter and calls it with
`call` (a structured skill call with a validated result) or `ask` (free text,
for any A2A agent). Fan out with plain `asyncio.TaskGroup`:

```python
import asyncio
from typing import Literal

from agentframe import Agent, Remote, RunContext, agent, skill

type Research = Remote[Literal["research-agent"]]


@agent(name="Planner", version="0.1.0")
class Planner(Agent):
    """Plans a goal with help from a research agent."""

    @skill(id="plan", tags=["planning"])
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

Each call carries the remaining deadline, its token ceiling, the hop count and
the caller's identity. To let the model choose which agents to call instead,
read the
[orchestration guide](https://github.com/malcmiller/agentframe/blob/main/docs/guides/orchestration.md).

## Examples

Each example has a `create_app()` you can serve with
`agentframe run examples.<name>:create_app`. Model-backed examples choose
Ollama or Foundry from `AGENTFRAME_MODEL_PROVIDER`, as in
[using a model](#using-a-model).

| Example | Shows |
| --- | --- |
| [`examples/echo.py`](https://github.com/malcmiller/agentframe/blob/main/examples/echo.py) | A model-free agent with an injected service, hosted for callers with a development API key. |
| [`examples/assistant.py`](https://github.com/malcmiller/agentframe/blob/main/examples/assistant.py) | Text generation, a typed structured output and a model-free skill on one agent. |
| [`examples/orders.py`](https://github.com/malcmiller/agentframe/blob/main/examples/orders.py) | Tool calling with permissions and a tool policy, behind a development API key. |
| [`examples/routed_assistant.py`](https://github.com/malcmiller/agentframe/blob/main/examples/routed_assistant.py) | Free text routed to a skill by the built-in `ModelRouter`. |
| [`examples/multi_agent`](https://github.com/malcmiller/agentframe/tree/main/examples/multi_agent) | A planner that fans out structured calls and free-text asks to a research agent over A2A, and lets its model plan and run a post over the research and writer agents, with a planner evaluation. Run it with `agentframe dev`. |
| [`examples/plan_approval.py`](https://github.com/malcmiller/agentframe/blob/main/examples/plan_approval.py) | Human approval of model-made plans: one skill proposes a plan, a permission-protected skill runs the reviewed plan, with a plan check. |

[`examples/smoke.py`](https://github.com/malcmiller/agentframe/blob/main/examples/smoke.py)
serves every example with a scripted model and checks each returns a complete,
validated result: `uv run python -m examples.smoke`.

## Documentation

- [Documentation index](https://github.com/malcmiller/agentframe/blob/main/docs/README.md)
- [API reference](https://github.com/malcmiller/agentframe/blob/main/docs/api/README.md)
- [Agent examples: model-free, model-backed and mixed](https://github.com/malcmiller/agentframe/blob/main/docs/guides/agent-examples.md)
- [Decorators and type annotations](https://github.com/malcmiller/agentframe/blob/main/docs/guides/annotations.md)
- [Local development and containers](https://github.com/malcmiller/agentframe/blob/main/docs/guides/development.md)
- [Execution limits and token accounting](https://github.com/malcmiller/agentframe/blob/main/docs/guides/limits.md)
- [Authorization and caller identity](https://github.com/malcmiller/agentframe/blob/main/docs/guides/authorization.md)
- [Orchestration](https://github.com/malcmiller/agentframe/blob/main/docs/guides/orchestration.md)
- [Evaluating agents](https://github.com/malcmiller/agentframe/blob/main/docs/guides/evaluation.md)
- [Roadmap](https://github.com/malcmiller/agentframe/blob/main/docs/roadmap.md)
- [Changelog](https://github.com/malcmiller/agentframe/blob/main/CHANGELOG.md)

## Contributing

Contributions are welcome. See
[CONTRIBUTING.md](https://github.com/malcmiller/agentframe/blob/main/CONTRIBUTING.md)
for setup, the checks every change must pass and how pull requests work.

## Security

Report vulnerabilities privately as described in
[SECURITY.md](https://github.com/malcmiller/agentframe/blob/main/SECURITY.md).

## License

Licensed under the
[Apache License 2.0](https://github.com/malcmiller/agentframe/blob/main/LICENSE).
