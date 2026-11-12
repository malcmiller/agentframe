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
from a2a.helpers import new_data_part
from a2a.types import (
    Message,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    Task,
)

from agentframe.card import SKILL_CALL_EXTENSION_URI


class APIKeyCredentials(CredentialService):
    """Hands one API key to the SDK for whatever scheme the card declares."""

    def __init__(self, key: str) -> None:
        self.key = key

    async def get_credentials(
        self, security_scheme_name: str, context: ClientCallContext | None
    ) -> str:
        return self.key


async def connect(base_url: str, api_key: str | None = None) -> Client:
    """Connect like any A2A caller: resolve the public card, then use it."""
    interceptors: list[ClientCallInterceptor] = []
    if api_key is not None:
        interceptors.append(AuthInterceptor(APIKeyCredentials(api_key)))
    return await ClientFactory(ClientConfig()).create_from_url(
        base_url, interceptors=interceptors
    )


async def send(
    client: Client,
    message: Message,
    *,
    skill_call: bool,
    return_immediately: bool = False,
) -> Task:
    """Send one message and return the task; ``skill_call`` activates the extension."""
    context = None
    if skill_call:
        extensions = with_a2a_extensions([SKILL_CALL_EXTENSION_URI])
        context = ClientCallContext(
            service_parameters=ServiceParametersFactory.create([extensions])
        )
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
