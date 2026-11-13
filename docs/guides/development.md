# Local development and container packaging

`agentframe run` (M2), `agentframe dev` (M8+ #95) and `agentframe compose`
(M8+ #96) are implemented.

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
  logged, and are gone when `dev` exits. Like any environment variable, they
  are visible to other processes of the same OS user (for example with
  `ps eww`);
- starts each agent as its own `agentframe run` process, in file order, each
  after the previous one answers `/ready`. List the agents another agent calls
  before it, since its startup checks that they can be reached;
- prints one combined log: every line is prefixed `[agent-name]`, and each
  run logs `Run started` / `Run completed` with its invocation ID and root
  invocation ID, so one request can be followed across agents.

Every agent also accepts `AGENTFRAME_DEV_CLIENT_KEY`, when set in the
environment `dev` runs in, as caller `developer`: that is how you, a script
or a test call an agent (`X-API-Key: my-local-key`). Without it, only the
agents can call each other. An `AGENTFRAME_AGENTS` registry exported in your
shell is not passed on; the agents find each other only through the generated
registry.

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

Implemented (#96). Package **one container per agent**. From the directory of
`agentframe.yaml`:

```shell
agentframe compose
```

writes a development `compose.yaml` and, if you have none, a `Dockerfile`.
`--force` overwrites an existing `compose.yaml`; an existing `Dockerfile` is
never changed. The command only writes files: it does not build, start or
push anything, and AgentFrame does not provision or manage infrastructure.

The `Dockerfile` template installs your project's wheel from `dist/` on a
`python:3.13-slim` image pinned by digest and runs as a non-root user:

```dockerfile
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY dist/*.whl /tmp/wheels/
RUN python -m pip install --no-cache-dir /tmp/wheels/*.whl && rm -rf /tmp/wheels
USER 10001:10001
```

Your wheel must depend on `agentframe-py[server]`; build it into `dist/`
first (for example `uv build`). Update the digest deliberately, after testing.

Each agent becomes one service named after it. It runs
`agentframe run APP --host 0.0.0.0 --port PORT` (all interfaces inside its
container only), publishes the port on the host's `127.0.0.1` only, reports
healthy once `/ready` answers and starts after the agents listed before it
are healthy. It gets the same environment as under `agentframe dev`, with
service names instead of `127.0.0.1`, so the same factories work unchanged:

```yaml
# compose.yaml (excerpt for the planner)
environment:
  AGENTFRAME_DEV: '1'
  AGENTFRAME_PUBLIC_URL: http://planner:8002
  AGENTFRAME_AGENTS: research-agent=http://research-agent:8001/.well-known/agent-card.json,planner=http://planner:8002/.well-known/agent-card.json
  AGENTFRAME_ALLOW_HTTP_HOSTS: research-agent,planner
  AGENTFRAME_DEV_API_KEYS: research-agent=${RESEARCH_AGENT_API_KEY:?set RESEARCH_AGENT_API_KEY in .env},developer=${AGENTFRAME_DEV_CLIENT_KEY:?set AGENTFRAME_DEV_CLIENT_KEY in .env}
  AGENTFRAME_DEV_REMOTE_KEYS: research-agent=${PLANNER_API_KEY:?set PLANNER_API_KEY in .env}
```

`dev_settings()` reads `AGENTFRAME_PUBLIC_URL` into
`HostConfig(public_url=...)`, as in the factory above, so each agent's card
advertises the address other containers reach it at. Plain http between
containers works only through the explicit development opt-in:
`AGENTFRAME_DEV=1` plus `AGENTFRAME_ALLOW_HTTP_HOSTS`, which becomes
`AgentDirectory(allow_http_hosts=[...])`. Without it the directory refuses
those http URLs.

Keys appear only as `${...}` references. Create a `.env` file next to
`compose.yaml` with one different random value per variable (the command
prints the list; keep `.env` out of version control), then start:

```shell
python -c "import secrets; print(secrets.token_urlsafe(32))"  # once per key
uv build
docker compose up --build --wait
```

`AGENTFRAME_DEV_CLIENT_KEY`, required here, is how you call an agent from the host, as caller
`developer`, on `http://127.0.0.1:PORT`. The cards advertise service names,
which resolve only inside the Compose network, so an A2A client on the host
must send its requests to the published `127.0.0.1` ports instead.

**The generated file is for development only.** For production:

- configure real authentication (for example `EntraIDAuth`) and outbound
  credentials instead of `dev_settings()` and development API keys;
- serve HTTPS, with advertised public URLs callers can reach, and drop
  `allow_http_hosts`;
- keep secrets out of images and Compose files, supplied at runtime by your
  platform's secret store; environment variables are readable by anyone who
  can inspect the container.

Kubernetes/Foundry templates are later.

## Acceptance and unresolved details

M2 proves single-agent CLI startup with explicit host configuration. #95
proves local named discovery and authenticated calls between agents started
by `agentframe dev`, tagged combined logs and failure cleanup
(`tests/integration/test_agentframe_dev.py`). #96 proves the generated files against golden files and that the directory
refuses the Compose URLs without the development opt-in
(`tests/unit/test_compose.py`); the opt-in `docker` test
(`tests/integration/test_compose_containers.py`, `AGENTFRAME_TEST_DOCKER=1`)
builds and starts both containers and has the planner call the researcher.

See [milestones](../roadmap.md#delivery-milestones),
[discovery](../design/discovery.md) and [hosting reference](../api/hosting.md).
