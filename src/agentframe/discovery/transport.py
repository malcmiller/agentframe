"""The official SDK's JSON-RPC client transport, with a bound on reply size.

Used by ``Remote``; not a public API. The SDK reads a whole reply into memory
before parsing it, so a peer could make its caller hold any amount of data.
This transport reads at most ``max_reply_bytes`` and otherwise behaves like
the SDK's.
"""

import json
from typing import Any

import httpx
from a2a.client import ClientCallContext
from a2a.client.errors import A2AClientError
from a2a.client.transports import JsonRpcTransport
from a2a.client.transports.http_helpers import get_http_args, handle_http_exceptions
from a2a.types import AgentCard


class ReplyTooLargeError(A2AClientError):
    """A peer's reply was larger than the transport's ``max_reply_bytes``."""


class BoundedJsonRpcTransport(JsonRpcTransport):
    """``JsonRpcTransport`` that refuses replies over ``max_reply_bytes``.

    It always uses a shared client, which it never closes. Only the
    non-streaming requests ``Remote`` sends (``SendMessage``, ``GetTask``,
    ``CancelTask``) are bounded; streaming is not used.

    Args:
        httpx_client: The client to send requests with.
        agent_card: The peer's validated card.
        url: The peer's JSON-RPC interface URL.
        max_reply_bytes: The largest reply body to read.
    """

    def __init__(
        self,
        httpx_client: httpx.AsyncClient,
        agent_card: AgentCard,
        url: str,
        *,
        max_reply_bytes: int,
    ) -> None:
        super().__init__(httpx_client, agent_card, url, is_shared_httpx=True)
        self.max_reply_bytes = max_reply_bytes

    async def _send_request(
        self, payload: dict[str, Any], context: ClientCallContext | None = None
    ) -> dict[str, Any]:
        # Replaces the SDK's version (a2a-sdk 1.3.0), which reads the whole
        # body with httpx's non-streaming send.
        if self.httpx_client.is_closed:
            raise A2AClientError("HTTP client is closed")
        request = self.httpx_client.build_request(
            "POST", self.url, json=payload, **get_http_args(context)
        )
        with handle_http_exceptions():
            response = await self.httpx_client.send(request, stream=True)
            try:
                response.raise_for_status()
                body = await self._read_limited(response)
            finally:
                await response.aclose()
            data = json.loads(body)
        return self._check_jsonrpc_response(data)

    async def _read_limited(self, response: httpx.Response) -> bytes:
        # The exception carries no body content, which is untrusted.
        too_large = ReplyTooLargeError(
            f"Reply is larger than {self.max_reply_bytes} bytes"
        )
        declared = response.headers.get("Content-Length", "")
        if declared.isdigit() and int(declared) > self.max_reply_bytes:
            raise too_large
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > self.max_reply_bytes:
                raise too_large
        return bytes(body)
