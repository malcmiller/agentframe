# Local development and container packaging

`agentframe run` (M2) and `agentframe dev` (M8+ #95) are implemented. The
container section is an approved design contract, not installed tooling or
generated configuration.

## One agent: M2

`agentframe run` starts one agent with Uvicorn (`pip install
'agentframe-py[server]'`). Point it at your own `create_app()` function, which
returns a fully configured `AgentApp`; read secrets from environment variables
inside it, never from command-line flags:

```shell
agentframe run myapp.hosting:create_app --host 127.0.0.1 --port 8000
```

For quick local development, `--dev` serves an agent class with
`AllowAnonymous` on localhost only. It logs a warning and refuses any
non-localhost `--host`:

```shell
agentframe run myapp.agents:Echo --dev --reload
```

`--host` defaults to `127.0.0.1`, `--port` to `8000`, and `--reload` restarts on
source changes. Normal explicit `auth` rules apply: the command never chooses
credentials or enables anonymous access outside `--dev`. Invalid targets,
factory return values and agent definitions are reported before anything is
served. Importing or defining an agent never starts a server. See the
[reference](../api/hosting.md#agentframe-run).

## Multiple agents: M8+

Implemented (#95). `agentframe.yaml` names each agent, its app factory (the
same `module:function` target `agentframe run` takes) and its port:

```yaml
# agentframe.yaml
agents:
  research-agent:
    app: myapp.research:create_app
    port: 8001
  planner:
    app: myapp.planner:create_app
    port: 8002
```

```shell
AGENTFRAME_DEV_CLIENT_KEY=my-local-key agentframe dev
```

`agentframe dev` reads the file in the working directory and:

- checks every port is free on `127.0.0.1` before starting anything;
- writes `.agentframe/agents.yaml` next to `agentframe.yaml`: a
  [registry file](../api/discovery.md#fileagentregistry) of each agent's name
  and local Agent Card URL, no keys. The folder ignores itself in git;
- generates one random API key per agent for this session. An agent calls the
  others with its own key and they verify it as that agent's name, so a
  specialist sees `ctx.principal.id == "planner"`. Keys reach the agents only
  through their environment; they are never written to disk, printed or
  logged, and are gone when `dev` exits;
- starts each agent as its own `agentframe run` process, in file order, each
  after the previous one answers `/ready`. List the agents another agent calls
  before it, since its startup checks that they can be reached;
- prints one combined log: every line is prefixed `[agent-name]`, and each
  run logs `Run started` / `Run completed` with its invocation ID and root
  invocation ID, so one request can be followed across agents.

Every agent also accepts `AGENTFRAME_DEV_CLIENT_KEY`, when set in the
environment `dev` runs in, as caller `developer`: that is how you, a script
or a test call an agent (`X-API-Key: my-local-key`). Without it, only the
agents can call each other.

Each factory reads that wiring with
[`dev_settings()`](../api/hosting.md#dev_settings) and stays plain Python:

```python
# myapp/planner.py
from agentframe import AgentApp, HostConfig, dev_settings

from myapp.agents import Planner


def create_app() -> AgentApp:
    dev = dev_settings()  # Development only: refuses without AGENTFRAME_DEV=1.
    return AgentApp(
        agent=Planner(),
        host=HostConfig(public_url=dev.public_url, auth=dev.auth),
        directory=dev.directory,
        remotes=dev.remotes,
    )
```

Only `Service(credential=APIKeyCredential(...))` identity is used between
development agents; on-behalf-of needs real Microsoft Entra ID and is never
fabricated. Development keys are not production defaults: outside `dev`,
configure real authentication and discovery.

Any failure (an invalid file, a port in use, an import error, an agent that
exits or is not ready within `--startup-timeout`, default 30 seconds) stops
every agent already started and exits with code 1 and one
`agentframe: error: ...` line. Ctrl+C or SIGTERM sends SIGTERM to every agent,
which shuts down gracefully (M7), then kills any still running after 35
seconds. If `dev` itself is killed with SIGKILL, its agents keep running. Dev
orchestration is not an infrastructure service or durable workflow engine.
See the [reference](../api/hosting.md#agentframe-dev).

## Containers

Package **one container per agent**. M8+ supplies a documented Dockerfile template
and a docker-compose example generated from `agentframe.yaml`; generation
command/flags, image/build details and production credential wiring remain open.
The illustrative future template below assumes the application has built an
installable wheel; none exists in this repository yet.

```dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY dist/*.whl /tmp/wheels/
RUN python -m pip install --no-cache-dir /tmp/wheels/*.whl
USER 10001:10001
CMD ["agentframe", "run", "myapp.research:create_app"]
```

Pin a tested base image/digest and dependencies when implementing packaging.
For containers, configure binding, explicit host auth, service credentials,
advertised reachable URLs and runtime secrets outside the image. `localhost`
inside one container is not another container's address. Development auto-key
behavior is not a production Compose security configuration.

An illustrative generated Compose shape (not a settled output schema):

```yaml
services:
  research-agent:
    build: .
    command: ["agentframe", "run", "myapp.research:create_app"]
    ports: ["127.0.0.1:8001:8001"]
  planner:
    build: .
    command: ["agentframe", "run", "myapp.planner:create_app"]
    ports: ["127.0.0.1:8002:8002"]
```

Ports here come from the illustrative `agentframe.yaml`; bind/port/auth config
must be wired by the future generator before this could run. It is not a
ready-to-deploy or anonymously accessible service definition.

Kubernetes/Foundry templates are later. AgentFrame does not provision, deploy
or manage infrastructure.

## Acceptance and unresolved details

M2 proves single-agent CLI startup with explicit host configuration. #95
proves local named discovery and authenticated calls between agents started
by `agentframe dev`, tagged combined logs and failure cleanup
(`tests/integration/test_agentframe_dev.py`). Equivalent independent
one-container-per-agent A2A composition is still to be proven (#96).

See [milestones](../roadmap.md#delivery-milestones),
[discovery](../design/discovery.md) and [hosting reference](../api/hosting.md).
