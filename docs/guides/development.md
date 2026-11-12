# Local development and container packaging

`agentframe run` (M2) is implemented. The multi-agent and container sections
are approved design contracts, not installed commands, generated configuration
or working deployment tooling.

## One agent: M2

`agentframe run` starts one agent with Uvicorn (`pip install
'agentframe[server]'`). Point it at your own `create_app()` function, which
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

**Decided:** `agentframe.yaml` lists named agents, module paths and ports.
The following is an illustrative schema, not finalized key names:

```yaml
agents:
  research-agent:
    module: myapp.research:ResearchAgent
    port: 8001
  planner:
    module: myapp.planner:Planner
    port: 8002
```

```shell
agentframe dev
```

`dev` reads that file and starts all agents on localhost, writes a local registry
file so they resolve each other by name, and sets up development API-key auth
between them. It emits one combined log tagged with agent name and invocation ID.
Service identity is explicit for these API-key calls; no user/OBO identity is
fabricated. Development credentials are not production defaults.

Final config keys, local registry path, key storage/rotation/disposal, app
bootstrap, subprocess ownership, port conflicts and startup/cleanup failure
semantics remain open. Never store generated keys in the public registry or
logs, overwrite user registry files silently, or leave partially started agents
without an explicit error and bounded cleanup. Dev orchestration is not an
infrastructure service or durable workflow engine.

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
CMD ["agentframe", "run", "myapp.research:ResearchAgent"]
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
    command: ["agentframe", "run", "myapp.research:ResearchAgent"]
    ports: ["127.0.0.1:8001:8001"]
  planner:
    build: .
    command: ["agentframe", "run", "myapp.planner:Planner"]
    ports: ["127.0.0.1:8002:8002"]
```

Ports here come from the illustrative `agentframe.yaml`; bind/port/auth config
must be wired by the future generator before this could run. It is not a
ready-to-deploy or anonymously accessible service definition.

Kubernetes/Foundry templates are later. AgentFrame does not provision, deploy
or manage infrastructure.

## Acceptance and unresolved details

M2 proves single-agent CLI startup with explicit host configuration. M8+ proves
local named discovery and authenticated calls between N agents, tagged combined
logs, failure cleanup, and equivalent independent one-container-per-agent A2A
composition. No claim of implementation follows from syntax validation here.

See [milestones](../roadmap.md#delivery-milestones),
[discovery](../design/discovery.md) and [hosting reference](../api/hosting.md).
