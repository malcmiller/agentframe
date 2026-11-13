"""Development wiring for agents started by ``agentframe dev``.

``agentframe dev`` starts each agent as its own ``agentframe run`` process and
passes it, through environment variables, everything it needs to be called
by and to call the other agents:

``AGENTFRAME_DEV``
    ``1``: development mode. ``dev_settings()`` refuses to run without it.
``AGENTFRAME_PUBLIC_URL``
    The agent's own address, such as ``http://127.0.0.1:8001``.
``AGENTFRAME_DEV_REGISTRY``
    The generated registry file of agent names and local Agent Card URLs.
``AGENTFRAME_DEV_API_KEYS``
    The API keys this agent accepts, as comma-separated ``caller=key`` pairs.
``AGENTFRAME_DEV_REMOTE_KEYS``
    The API key to call each other agent with, as ``name=key`` pairs.
``AGENTFRAME_ALLOW_HTTP_HOSTS``
    Optional comma-separated hosts whose cards may be fetched over plain
    http, for example container service names.

An app factory turns them into its configuration with ``dev_settings()``, so
it stays plain Python.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from agentframe.discovery.directory import AgentDirectory
from agentframe.discovery.identity import APIKeyCredential, Service
from agentframe.discovery.registry import FileAgentRegistry
from agentframe.errors import DefinitionError
from agentframe.security.authentication import APIKey, APIKeyAuth

DEV_VARIABLE = "AGENTFRAME_DEV"
PUBLIC_URL_VARIABLE = "AGENTFRAME_PUBLIC_URL"
REGISTRY_VARIABLE = "AGENTFRAME_DEV_REGISTRY"
API_KEYS_VARIABLE = "AGENTFRAME_DEV_API_KEYS"
REMOTE_KEYS_VARIABLE = "AGENTFRAME_DEV_REMOTE_KEYS"
ALLOW_HTTP_HOSTS_VARIABLE = "AGENTFRAME_ALLOW_HTTP_HOSTS"


@dataclass(frozen=True)
class DevSettings:
    """An agent's development configuration, from ``dev_settings()``.

    Never shows a key in its ``repr``.

    Args:
        public_url: The agent's own address, for ``HostConfig(public_url=...)``.
        auth: Accepts the other agents' development API keys, for
            ``HostConfig(auth=...)``. Each caller is verified as its agent name.
        directory: Finds the other agents by name, for ``AgentApp(directory=...)``;
            ``None`` when no registry was given.
        remotes: The development API key to call each other agent with, for
            ``AgentApp(remotes=...)``.
    """

    public_url: str
    auth: APIKeyAuth
    directory: AgentDirectory | None
    remotes: Mapping[str, Service]


def dev_settings() -> DevSettings:
    """Read the development configuration ``agentframe dev`` passes an agent.

    Use it in an app factory::

        def create_app() -> AgentApp:
            dev = dev_settings()
            return AgentApp(
                agent=Planner(),
                host=HostConfig(public_url=dev.public_url, auth=dev.auth),
                directory=dev.directory,
                remotes=dev.remotes,
            )

    The directory has its own HTTP client, open for the life of the process,
    because an app factory has no ``async with`` to open it in.

    Returns:
        The agent's address, authentication, directory and remotes.

    Raises:
        DefinitionError: ``AGENTFRAME_DEV`` is not ``1``, a required variable
            is missing, or a variable is malformed. Messages never contain keys.
    """
    if os.environ.get(DEV_VARIABLE) != "1":
        raise DefinitionError(
            f"dev_settings() is for development only and needs {DEV_VARIABLE}=1, "
            "which agentframe dev sets. Configure real authentication and "
            "discovery outside development."
        )
    accepted = read_pairs(API_KEYS_VARIABLE)
    if not accepted:
        raise DefinitionError(
            f"{API_KEYS_VARIABLE} lists no keys, so nothing could call this agent."
        )
    auth = APIKeyAuth(
        keys={key: APIKey(caller_id=caller) for caller, key in accepted.items()}
    )
    remotes = {
        name: Service(credential=APIKeyCredential(key))
        for name, key in read_pairs(REMOTE_KEYS_VARIABLE).items()
    }
    return DevSettings(
        public_url=_required(PUBLIC_URL_VARIABLE),
        auth=auth,
        directory=_directory(),
        remotes=remotes,
    )


def format_pairs(pairs: Mapping[str, str]) -> str:
    """Return ``pairs`` as comma-separated ``name=value`` text for ``read_pairs``."""
    return ",".join(f"{name}={value}" for name, value in pairs.items())


def read_pairs(variable: str) -> dict[str, str]:
    """Read an environment variable of comma-separated ``name=value`` pairs.

    Args:
        variable: The variable's name. Unset or empty means no pairs.

    Returns:
        The values by name.

    Raises:
        DefinitionError: A pair has no ``=``, an empty side, or a repeated
            name. The message never contains a value, since values are keys.
    """
    pairs: dict[str, str] = {}
    for entry in os.environ.get(variable, "").split(","):
        if not entry.strip():
            continue
        name, separator, value = (part.strip() for part in entry.partition("="))
        if not separator or not name or not value or name in pairs:
            raise DefinitionError(
                f"{variable} must hold comma-separated name=key pairs with "
                "unique names."
            )
        pairs[name] = value
    return pairs


def _required(variable: str) -> str:
    value = os.environ.get(variable, "").strip()
    if not value:
        raise DefinitionError(f"{variable} is not set; agentframe dev sets it.")
    return value


def _directory() -> AgentDirectory | None:
    registry = os.environ.get(REGISTRY_VARIABLE, "").strip()
    if not registry:
        return None
    allowed = os.environ.get(ALLOW_HTTP_HOSTS_VARIABLE, "").split(",")
    return AgentDirectory(
        sources=[FileAgentRegistry(registry)],
        allow_http_hosts=[host.strip() for host in allowed if host.strip()],
        # Like the directory's own client: credentials are never followed
        # through a redirect.
        http_client=httpx.AsyncClient(follow_redirects=False),
    )
