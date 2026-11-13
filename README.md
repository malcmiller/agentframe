# AgentFrame

**Define agents with Python. Communicate through A2A. Choose your model host.**

AgentFrame is a Python library for creating agents through classes,
decorators, and type annotations. Agent-to-Agent (A2A) interoperability and
Agent Cards are core contracts, not optional exports.
The goal is a default authoring path for building one to many agents:
independently deployable specialists and orchestrator agents that cooperate
over A2A when needed, including with other frameworks' A2A agents.

AgentFrame is an independent agent framework, not a wrapper around LangChain,
LangGraph, Microsoft Agent Framework, Pydantic AI, Google ADK, CrewAI, or another
major agent package. It owns its authoring API and execution behavior while
reusing protocol SDKs, validation libraries, and model-provider clients.

## Status

AgentFrame 0.1.0, the first usable single-agent release (milestones M0 to M7),
is [published on PyPI](https://pypi.org/project/agentframe-py/0.1.0/). You can define agents with `@agent` / `@skill`, inject services, and run skills locally
through `Runtime.invoke` / `Runtime.run` with validated inputs and outputs,
permission checks and a timeout. M2 native A2A hosting is complete: `AgentApp`
serves an agent as an ASGI application with its Agent Card and an authenticated
A2A 1.0 endpoint, proven end to end with the official A2A client, and
`agentframe run` serves it with one command (see
[hosting over A2A](#hosting-over-a2a) and
[development](docs/guides/development.md#one-agent-m2)). M3 text generation is
complete: an injected `OllamaModel` (local Llama) or
`FoundryModel` (Foundry GPT deployment) serves the same agent, switched by
configuration only (see the [first success criterion](#first-success-criterion)).
M4 typed structured output is complete: `ctx.generate(text, output=Summary)`
returns a validated Pydantic instance with either provider, and hosted skills
return it as an A2A data part; invalid output, refusal and truncation fail
with typed errors, never fake success.
M5 bounded tool calling is complete: registered `@tool` functions selected by
`ctx.generate(text, tools=[...])` run until the model answers, within
`max_tool_calls`, with every call authorized before it runs and no tool ever
replayed by a model retry (see
[`examples/orders.py`](examples/orders.py)).
M6 built-in opt-in model routing is complete: with
`AgentApp(router=ModelRouter(model))`, free-text A2A messages are routed to a
skill with validated arguments, or the caller is asked a question in the same
task; the routing calls count toward the run's limits (see
[`examples/routed_assistant.py`](examples/routed_assistant.py)).
M7 hardening is in place: host limit ceilings, run admission, request and
response size limits, graceful shutdown with `/health` and `/ready`, A2A
message deduplication, `EntraIDAuth` and `ExecutionPolicy` checks, proven end
to end by `tests/integration/test_hardening.py`.
Multi-agent discovery and remote A2A composition (M8+), MCP, telemetry and
streaming are not implemented yet; documentation for them is labeled as planned
design, and their unspecified interfaces remain open.

## Installation

Install AgentFrame from PyPI (Python 3.13 or later):

```shell
pip install agentframe-py
```

The distribution is named `agentframe-py`, but you import it as `agentframe`
(like `scikit-learn` and `sklearn`).

Optional extras add what a deployment needs:

| Extra | Adds |
| --- | --- |
| `server` | Uvicorn, for the `agentframe run` command. |
| `ollama` | `OllamaModel`, for local Llama through Ollama. |
| `foundry` | `FoundryModel`, for Microsoft Foundry GPT deployments. |
| `entra` | `EntraIDAuth`, for Microsoft Entra ID caller authentication. |

For example, `pip install "agentframe-py[server,ollama]"`.

**Stability:** Pre-1.0: public APIs may change in minor releases; changes are
listed in the [changelog](CHANGELOG.md).

## Goals

- Make a small agent easy to define with explicit Python declarations.
- Generate native A2A Agent Cards from agent and skill metadata plus host configuration.
- Support model-free agents and optional model-assisted behavior.
- Start with Microsoft Foundry model inference and local Llama through Ollama.
- Allow model selection to change without rewriting agent behavior.
- Provide safe, validated defaults with typed configuration and dependency injection.
- Build on the official A2A Python SDK instead of maintaining another wire format.
- Build and deploy 1 to N agents; compose specialists through ordinary orchestrator
  skills using interoperable A2A calls, not a required workflow/graph engine.

## Authoring experience

Decorators describe the agent; an `Agent` subclass implements behavior. Models
and infrastructure are injected. Each ability is a separate `@skill` method; no
abstract `handle()` or manual dispatch method is required. This model-free agent
is [`examples/echo.py`](examples/echo.py); run the `await` lines inside an async
function or `asyncio.run`:

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

Names default to the class/method name and descriptions to docstrings.

### Hosting over A2A

Serve the same agent to any A2A 1.0 client with a `create_app()` function:
[`examples/echo.py`](examples/echo.py) wraps `Echo` in an `AgentApp` with a
development API key and authorization policy. These are **development
placeholders**: production hosts need your real identity provider and resource
checks. Remote hosts always require an explicit `auth=` choice.

Run it with Uvicorn from the `server` extra (`uv sync --extra server` from
source):

```shell
ECHO_API_KEY=dev-only-key agentframe run examples.echo:create_app
```

The Agent Card is served at `/.well-known/agent-card.json` and the JSON-RPC
endpoint at `/a2a`; callers send the key in the `X-API-Key` header. Callers can
name a skill with JSON arguments through the
[skill-call extension](docs/api/hosting.md#structured-skill-call-wire-format),
or send free text to the built-in [`ModelRouter`](docs/api/hosting.md#modelrouter)
or a custom [`SkillRouter`](docs/api/hosting.md#skillrouter).
`tests/integration/test_interoperability.py` drives every case with the official A2A client.

For production identity in a Microsoft environment, replace the API key with
[`EntraIDAuth`](docs/api/authorization.md#entraidauth) (install the `entra`
extra). Callers send a Microsoft Entra v2.0 access token as
`Authorization: Bearer <token>`; their app roles and scopes map to permissions:

```python
from agentframe import EntraIDAuth, HostConfig

host = HostConfig(
    public_url="https://writer.example.com",
    # audience is the API's application (client) ID, not its api:// URI.
    auth=EntraIDAuth(tenant_id=tenant_id, audience=writer_api_client_id),
    role_permissions={"Editor": ["documents:write"], "Documents.Read": ["documents:read"]},
)
```

`tenant_id` and `writer_api_client_id` come from your app registration; the
reference page lists the one-time setup.

The normal path needs only one AgentFrame subclass, `Agent`. Model-free agents
need no provider configuration. See [agents with and without models](docs/guides/agent-examples.md)
for multi-skill, mixed, and local invocation examples, and the
[API design examples](docs/design/base-classes.md#proposed-api-examples) for host
composition and advanced extension points.

Llama is a model family; Ollama is the initial local model host. Foundry model
inference is distinct from deploying an agent to Foundry Agent Service.

## First success criterion

M1 is the first proof: model-free authoring and managed `Runtime.invoke`.
M2 adds native A2A hosting with custom routing only. M3 meets this success
criterion:

Define one text agent in approximately 25 lines of agent-authoring code, generate
and publish a valid Agent Card, invoke it with an independent official A2A client,
and switch between a Foundry GPT deployment and local Llama by changing only model
configuration. Host bootstrap and credential setup are documented separately.

The same public contract must work with both hosts; identical generated answers
are not required. A deterministic model-free example must work without cloud
credentials, model downloads, or an inference server.

This works from source today. The agent in
[`examples/assistant.py`](examples/assistant.py) has a text skill, a typed
structured-output skill and a model-free skill, and never names a model host.
Its text skill is all a text agent needs:

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

Its `create_app()` reads `AGENTFRAME_MODEL_PROVIDER` and builds an
`OllamaModel` or a `FoundryModel` from environment variables; endpoints and
names never live in code. From a source checkout (`uv sync` installs the
server and provider extras for development), serve it with local Llama through
Ollama (the model tag must already be installed; nothing is downloaded):

```shell
AGENTFRAME_MODEL_PROVIDER=ollama AGENTFRAME_OLLAMA_MODEL=llama3.2:3b \
    agentframe run examples.assistant:create_app
```

or with a Foundry GPT deployment, signed in with Microsoft Entra ID through the
Azure CLI (`az login`; no API key):

```shell
AGENTFRAME_MODEL_PROVIDER=foundry \
    AGENTFRAME_FOUNDRY_ENDPOINT=https://<resource>.openai.azure.com/openai/v1/ \
    AGENTFRAME_FOUNDRY_DEPLOYMENT=<deployment> \
    agentframe run examples.assistant:create_app
```

`AGENTFRAME_OLLAMA_ENDPOINT` optionally overrides `http://localhost:11434`.
Each provider is imported only when selected, so an install with one provider
extra is enough. A2A clients apply their own request timeout (5 seconds by
default in the official `a2a-sdk` client), which a model call can exceed.
The example accepts anonymous callers as a **development placeholder** for a
localhost server; production hosts need real authentication and authorization
(see [hosting over A2A](#hosting-over-a2a)). Any A2A 1.0 client can fetch the
card and call `answer`, `summarize` or `count_words` through the
[skill-call extension](docs/api/hosting.md#structured-skill-call-wire-format);
`summarize` returns one data part `{"value": {"text": ..., "key_points": [...]}}`,
and the completed task's metadata reports the run's token usage.
`tests/integration/test_success_criterion.py` proves this offline in CI: the official A2A
client calls the same agent served with two different scripted models, with no
code change, and `count_words` runs without inference.
`tests/integration/test_structured_example.py` proves `summarize` the same way,
including safe `FAILED` tasks for invalid output, refusal, truncation and a
model without structured output. The opt-in
[real-provider tests](AGENTS.md#real-provider-tests-opt-in-local-only) run the
same contract against Ollama and Foundry.

## Initial scope

The first usable release (M0-M7) focuses on a single agent: authoring, native
A2A hosting, model adapters, text generation, typed structured outputs, and bounded execution of
explicitly registered tools with generation continuations. It returns complete
validated results, not streamed partial output. Multi-skill A2A supports a
built-in opt-in model-based router plus custom routers; injecting a model alone
never enables model routing (`AgentApp(router=ModelRouter(model))` opts in).
The generation calls are decided: `ctx.generate(text)` for text,
`ctx.generate(text, output=Summary)` for validated Pydantic output and
`ctx.generate(text, tools=[registered_tool])` for bounded continuations.
One server/shared runtime serves audience-specific endpoints, not per-skill
routes: A2A for agents, optional MCP for LLM apps/tools and AG-UI later for
frontends. Prefer structured A2A skill/JSON-argument requests through a declared,
activated extension; free text uses routing and input-required clarification.
Structured selection bypasses routing, not authorization or execution controls.
See the [request-style design and open extension contract](docs/design/mcp-skills.md#a2a-request-styles-and-extension-boundary).

The [delivery plan](docs/roadmap.md#delivery-milestones) splits this work
into independently releasable/testable milestones: M0 development setup, M1
model-free execution, M2 A2A/custom routing, M3 interchangeable text models,
M4 structured outputs, M5 bounded tools, M6 built-in opt-in model routing
(after M3/M4), and M7 hardening to the first usable release.

The application owns human approval workflows; the first release has no built-in
approval pause/resume. Tool authorization/action checks remain mandatory.
Protocol task support is not a resumable human workflow. The current core does
not include a UI, durable workflow/graph engine, hosting/deployment service,
federation, RAG system, evaluation platform, or Foundry-hosted agent deployment.
Revisit durable workflows after the multi-agent release based on real usage;
no design now. If added, use an optional separate package (candidate
`agentframe-workflows`). Long work uses A2A tasks with an in-memory default
and pluggable persistent store, not automatic checkpoint/recovery or approvals.

Token budgets and deadlines are runtime-enforced configuration. Tracing
integration and MCP are separate optional follow-ups after M7. OpenTelemetry
content capture is off by default; MCP publishes explicitly allowlisted skills,
not every skill or internal tool. A custom REST skill API is deferred.

Skills may declare required permissions; application roles map to grants outside
the agent definition. The runtime enforces verified identity and
application resource policy across entry points. Prompt safeguarding combines
library trust boundaries with application checks, not a guaranteed injection filter.

The [runtime contracts](docs/api/runtime.md) distinguish `invoke` (validated
value) from `run` (`RunResult` with local metadata even without tracing).
Use [annotated `Inject[T]` and `Services`](docs/api/agents.md#injectt) for service
dependencies, validated at startup and excluded from caller schemas.
[Decided defaults](docs/guides/limits.md) apply within independent host ceilings;
remote hosts require an explicit [authenticator](docs/api/authorization.md#authenticator),
never an implicit anonymous mode. Safe typed errors carry invocation correlation;
transient model retries and A2A deduplication never promise exactly-once actions.

**The next core release (M8+) is multi-agent A2A composition**, not an optional
discovery add-on. Read-first registries and a managed directory provide validated
references, native card resolution, five-minute caching and stale refresh.
Skills receive `Remote["name"]` bindings: `call("skill", ..., output=Type)`
uses structured A2A, while `ask(text)` uses plain A2A text. Both await completion;
input-required raises `ClarificationNeededError`, never automatic back-and-forth.
Orchestrators are ordinary AgentFrame
agents whose skills call specialists, not a workflow/graph engine. The release
must carry identity/delegation, permissions, deadlines, remaining token allowance
and trace/correlation across hops without amplifying authority or overriding
remote services' limits. Per-remote Entra OBO or explicit service identity,
remaining deadline/token ceilings and default maximum 5 hops constrain calls.
No orchestration helpers in the first multi-agent release: use plain Python
`await`, `asyncio.TaskGroup` and `directory.find(skill=... / tags=...)`.
Typed `Remote[ResearchAgent]` proxies are a later option, not proprietary RPC.

M2 adds `agentframe run module:create_app` (and `module:Class --dev` for
local anonymous development); M8+ adds `agentframe dev` reading
`agentframe.yaml`, local registry/development API-key wiring and tagged combined
logs. Package one container per agent, with a Dockerfile template and generated
Compose example; Foundry/Kubernetes templates are later. See the
[development plan](docs/guides/development.md). AgentFrame does not become a
hosting platform. Discovery itself does not deploy agents, grant authority or
invoke them automatically. Foundry registry discovery uses the chosen projects
agents API (`azure-ai-projects` 2.x); A2A-enabled filtering, card mapping,
auth/RBAC and pagination still need implementation proof. See
[discovery design](docs/design/discovery.md) and the [reference](docs/api/discovery.md).

Streaming is a separate follow-up after M7, planned to support **AG-UI** as an
optional agent-to-frontend
protocol adapter, distinct from A2A agent-to-agent and MCP tool interfaces.
A2A/cards stay first-class; no UI or major agent-framework dependency is implied.
`AgentApp(ag_ui=AGUIConfig(enabled=True))` will serve `/ag-ui` with shared
auth/limits/permissions: lifecycle, text deltas, tool names/status and safe errors.
Whole-answer checks send complete text at end; disconnect cancels, with no
first-version reconnect/resume. Frontend tools/shared state are later opt-in.
The [streaming proposal](docs/design/streaming.md) scopes event mapping, trust and
output-policy boundaries without claiming implemented AG-UI conformance or
automatically advertising A2A streaming.

The multi-agent release follows the single-agent M7 release. A larger hosted
platform or durable workflow engine is not a prerequisite for either.

## Runnable examples

Each example is a short, typed module with a `create_app()` you can serve with
`agentframe run examples.<name>:create_app`. Model-backed examples choose
Ollama or Foundry from `AGENTFRAME_MODEL_PROVIDER` (see the
[first success criterion](#first-success-criterion)); their
`create_app(model=...)` also accepts any model, such as a test model.

| Example | Shows |
| --- | --- |
| [`examples/echo.py`](examples/echo.py) | A model-free agent with an injected service, hosted for callers with a development API key. |
| [`examples/assistant.py`](examples/assistant.py) | Text generation, a typed structured output and a model-free skill on one agent. |
| [`examples/orders.py`](examples/orders.py) | Tool calling with permissions and a tool policy, behind a development API key. |
| [`examples/routed_assistant.py`](examples/routed_assistant.py) | Free text routed to a skill by the built-in `ModelRouter`. |

[`examples/smoke.py`](examples/smoke.py) serves every example in-process with
a scripted model and calls it with the official A2A client, checking each
returns a complete, validated result. CI runs it against the installed wheel;
run it yourself from the repository root with `uv run python -m examples.smoke`.

## Documentation

- [Documentation index](docs/README.md)
- [Proposed API reference: public surfaces, parameters, and open contracts](docs/api/README.md)
- [Decided local results and safe error contracts](docs/api/runtime.md)
- [Service injection and lifecycle](docs/api/agents.md#injectt)
- [Roadmap and delivery milestones](docs/roadmap.md)
- [Decorators and type annotations](docs/guides/annotations.md)
- [Base classes and interfaces](docs/design/base-classes.md)
- [Agent examples: model-free, model-backed, and mixed](docs/guides/agent-examples.md)
- [Read-first registries and managed agent discovery](docs/design/discovery.md)
- [Local CLI development and container packaging plan](docs/guides/development.md)
- [Future streaming and the optional AG-UI adapter](docs/design/streaming.md)
- [Execution limits and token accounting](docs/guides/limits.md)
- [Tracing and correlation](docs/design/tracing.md)
- [A2A hosting and optional MCP skill configuration](docs/design/mcp-skills.md)
- [Skill authorization and caller identity](docs/guides/authorization.md)
- [Prompt safeguarding and shared responsibilities](docs/design/prompt-safeguarding.md)
- [Contributor and coding-agent context](AGENTS.md)

## Development and contributions

To work on AgentFrame itself, install it from source as below. Proposals
should connect to the first success criterion and identify interoperability,
safety, and authoring tradeoffs.
Human readability is a top-priority contribution rule: prefer simple,
maintainable code over cleverness or speculative abstractions. Use uv 0.13.1
(install with `brew install uv` on macOS, checking the installed version).
From a fresh clone, one command sets up the package and development environment:

```bash
uv sync
```

Python is downloaded if needed. The default interpreter is Python 3.13.
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

All four checks must pass before committing: `uv run ruff format --check`,
`uv run ruff check`, `uv run mypy` and `uv run pytest`. CI runs them on Python
3.13, 3.14 and 3.15, verifies an isolated wheel import and runs the
[examples smoke runner](#runnable-examples) against the installed wheel. Pre-commit runs Ruff
and mypy; tests run in CI, not on every commit. See
[code quality and readability](AGENTS.md#code-quality-and-readability) for the
contributor rules, local matrix commands and PyCharm setup.
Releases are published to PyPI by a workflow; see
[releasing](docs/guides/releasing.md).

### Real-provider tests

Tests against a real Ollama server or Foundry deployment are opt-in and never
run in normal CI; [AGENTS.md](AGENTS.md#real-provider-tests-opt-in-local-only)
lists the environment variables for running them locally. The manually triggered
`.github/workflows/foundry.yml` runs the `foundry` tests from GitHub, for example
before a release. It runs only from **Actions > Foundry provider tests > Run
workflow**, never on push or pull request, and waits for a reviewer to approve
the `foundry` environment. It signs in to Azure with OpenID Connect, so the
repository stores no secret; Foundry calls cost money, so run it sparingly.
There is no Ollama job, since the model download is large.

One-time setup, by a maintainer with rights to create an app registration and
assign roles on the Foundry resource (`<resource>` and `<resource-group>` are
your Foundry resource and its resource group):

1. Create an app registration and its service principal for the workflow:

   ```bash
   APP_ID=$(az ad app create --display-name agentframe-foundry-tests \
       --query appId -o tsv)
   SP_ID=$(az ad sp create --id "$APP_ID" --query id -o tsv)
   ```

2. Trust only this repository's `foundry` environment through a federated
   credential (no client secret). The subject must match the token's `sub`
   claim exactly. Repositories created after July 15, 2026, like this one,
   use [immutable subject claims](https://github.blog/changelog/2026-04-23-immutable-subject-claims-for-github-actions-oidc-tokens/)
   that include the owner and repository IDs (`repo:<owner>@<owner-id>/<repo>@<repo-id>`):

   ```bash
   az ad app federated-credential create --id "$APP_ID" --parameters '{
     "name": "agentframe-foundry-environment",
     "issuer": "https://token.actions.githubusercontent.com",
     "subject": "repo:malcmiller@58534574/agentframe@1452010815:environment:foundry",
     "audiences": ["api://AzureADTokenExchange"]
   }'
   ```

   For a fork, read your prefix with
   `gh api repos/<owner>/<repo>/actions/oidc/customization/sub --jq .sub_claim_prefix`
   (or the IDs with `gh api repos/<owner>/<repo> --jq '.owner.id, .id'`) and
   append `:environment:foundry`. If sign-in fails with **AADSTS700213** (no
   matching federated identity), copy the `subject claim - ...` value from the
   workflow's Azure login log into the federated credential.

3. Grant inference only, on the one Foundry resource. **Cognitive Services
   OpenAI User** allows calling deployments (including the Responses API) but
   not managing them; do not use broader roles such as Contributor. If calls
   still fail with 403 after the assignment propagates (a few minutes), assign
   **Foundry User** (formerly Azure AI User) on the same scope instead:

   ```bash
   RESOURCE_ID=$(az cognitiveservices account show --name <resource> \
       --resource-group <resource-group> --query id -o tsv)
   az role assignment create --assignee-object-id "$SP_ID" \
       --assignee-principal-type ServicePrincipal \
       --role "Cognitive Services OpenAI User" --scope "$RESOURCE_ID"
   ```

4. In the GitHub repository, open **Settings > Environments**, create an
   environment named `foundry`, enable **Required reviewers** (add yourself)
   and limit **Deployment branches and tags** to `main`.
5. Add these **environment variables** (not secrets; they are identifiers) to
   `foundry`:

   | Variable | Value |
   | --- | --- |
   | `AZURE_CLIENT_ID` | `$APP_ID` from step 1. |
   | `AZURE_TENANT_ID` | `az account show --query tenantId -o tsv` |
   | `AZURE_SUBSCRIPTION_ID` | The subscription containing the Foundry resource. |
   | `AGENTFRAME_FOUNDRY_ENDPOINT` | OpenAI v1 base URL, `https://<resource>.openai.azure.com/openai/v1/` or `https://<resource>.services.ai.azure.com/openai/v1/`. |
   | `AGENTFRAME_FOUNDRY_DEPLOYMENT` | Deployment name. |

   For example, `gh variable set AZURE_CLIENT_ID --env foundry --body "$APP_ID"`.

Then start the workflow on `main` and approve the pending `foundry` deployment.
A missing variable makes the tests fail rather than skip.
The [approved platform/API decisions](docs/roadmap.md#resolved-m0-platform-and-api-decisions)
(2026-11-11) are Python **3.13+**, testing through **3.15** (Python has no LTS);
A2A protocol **1.0** through official `a2a-sdk` (1.3.0 observed, specification
release v1.0.1); and Foundry **OpenAI v1 Responses API** through optional
`openai`, with injected Entra token-provider (`azure-identity`) or API-key
authentication. Do not use the retired Azure AI Inference SDK.
Foundry registry reads use the project agents API through `azure-ai-projects`
2.x (2.10.0 observed), separately from model inference and still in M8+.
These are decisions, not compatibility-test results or dependency pins.
Development tools and hatchling are pinned in `pyproject.toml`, with transitive
versions in `uv.lock`. M1 added the first runtime dependency, Pydantic
(`>=2.14,<3`). M2 adds the official A2A SDK as a core dependency,
`a2a-sdk[http-server]==1.3.0`, using the JSON-RPC over HTTP binding for protocol
1.0 only. Provider extras arrive in M3, pinned and verified when used; A2A native
type mappings and provider mappings remain implementation work.

The intended repository and distribution name is `agentframe`; the intended
Python import is `agentframe`. Package-index names are not reserved by creating
this repository, and no package availability or trademark clearance is guaranteed.

## References

- [A2A specification](https://a2a-protocol.org/latest/specification/)
- [Selected A2A specification release v1.0.1](https://github.com/a2aproject/A2A/releases/tag/v1.0.1)
- [Official A2A Python SDK](https://github.com/a2aproject/a2a-python)
- [Observed official Python SDK 1.3.0](https://pypi.org/project/a2a-sdk/1.3.0/)
- [Python release-cycle data](https://peps.python.org/api/release-cycle.json)
- [Ollama Python client](https://github.com/ollama/ollama-python)
- [OpenAI Python library](https://github.com/openai/openai-python)
- [Microsoft Foundry documentation](https://learn.microsoft.com/en-us/azure/ai-foundry/)
- [Foundry OpenAI SDK migration and Responses API](https://learn.microsoft.com/en-us/azure/foundry/how-to/model-inference-to-openai-migration)
- [Foundry incoming A2A and authenticated cards](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/enable-agent-to-agent-endpoint)
- [Foundry outbound A2A connections](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/agent-to-agent)

## License

Licensed under the [Apache License 2.0](LICENSE), a permissive open-source license
that includes an explicit patent grant subject to its terms.
