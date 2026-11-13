"""Call an ``AgentApp`` the way any A2A caller would: with the official SDK client.

Shared by the end-to-end tests. The client resolves the public Agent Card,
picks its JSON-RPC interface and authenticates with the scheme the card
declares; nothing here reaches into AgentFrame internals.
"""

import uuid
from collections.abc import Mapping

from a2a.client import (
    Client,
    ClientCallContext,
    ClientCallInterceptor,
    ClientConfig,
    ClientFactory,
)
from a2a.client.auth import AuthInterceptor, CredentialService
from a2a.client.service_parameters import (
    ServiceParametersFactory,
    with_a2a_extensions,
)
from a2a.helpers import new_data_part, new_text_part
from a2a.types import (
    Message,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    Task,
)

from agentframe.hosting.card import SKILL_CALL_EXTENSION_URI


class StaticCredentials(CredentialService):
    """Hands one API key or bearer token to the SDK for any declared scheme."""

    def __init__(self, credential: str) -> None:
        self.credential = credential

    async def get_credentials(
        self, security_scheme_name: str, context: ClientCallContext | None
    ) -> str:
        return self.credential


async def connect(base_url: str, credential: str | None = None) -> Client:
    """Connect like any A2A caller: resolve the public card, then use it.

    ``credential`` is an API key or bearer token; the SDK sends it the way the
    card's security scheme says.
    """
    interceptors: list[ClientCallInterceptor] = []
    if credential is not None:
        interceptors.append(AuthInterceptor(StaticCredentials(credential)))
    return await ClientFactory(ClientConfig()).create_from_url(
        base_url, interceptors=interceptors
    )


async def send(
    client: Client,
    message: Message,
    *,
    skill_call: bool,
    return_immediately: bool = False,
    timeout: float | None = None,
) -> Task:
    """Send one message and return the task; ``skill_call`` activates the extension.

    ``timeout`` overrides the client's request timeout (5 seconds by default)
    in seconds, for runs with several real model calls.
    """
    context = ClientCallContext(timeout=timeout)
    if skill_call:
        extensions = with_a2a_extensions([SKILL_CALL_EXTENSION_URI])
        context.service_parameters = ServiceParametersFactory.create([extensions])
    request = SendMessageRequest(
        message=message,
        configuration=SendMessageConfiguration(return_immediately=return_immediately),
    )
    [response] = [r async for r in client.send_message(request, context=context)]
    return response.task


async def call_skill(
    client: Client,
    skill_id: str,
    arguments: Mapping[str, object] | None = None,
    *,
    return_immediately: bool = False,
) -> Task:
    """Call a skill by ID with JSON arguments through the skill-call extension."""
    message = Message(
        message_id=str(uuid.uuid4()),
        role=Role.ROLE_USER,
        parts=[new_data_part(dict(arguments or {}))],
        metadata={"agentframe.skill": skill_id},
    )
    return await send(
        client, message, skill_call=True, return_immediately=return_immediately
    )


async def say(
    client: Client, text: str, task_id: str = "", *, timeout: float | None = None
) -> Task:
    """Send free text, optionally answering a question in an existing task."""
    message = Message(
        message_id=str(uuid.uuid4()),
        role=Role.ROLE_USER,
        parts=[new_text_part(text)],
        task_id=task_id,
    )
    return await send(client, message, skill_call=False, timeout=timeout)
