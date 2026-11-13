"""Bound the size of a request body before anything else reads it.

The app reads every ``/a2a`` body here first, counting bytes as they arrive,
so an oversized body is never held in full or parsed. A declared
``Content-Length`` above the limit is refused without reading anything;
chunked bodies, which declare no length, are refused as soon as the count
passes the limit.
"""

from starlette.requests import Request
from starlette.types import Message


async def read_body_within(request: Request, max_bytes: int) -> bytes | None:
    """Read ``request``'s body, or stop once it is larger than ``max_bytes``.

    Args:
        request: The incoming request, whose body has not been read yet.
        max_bytes: The largest body accepted.

    Returns:
        The whole body, or ``None`` when it is larger than ``max_bytes``.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > max_bytes:
            return None
    return bytes(body)


def with_body(request: Request, body: bytes) -> Request:
    """Return a copy of ``request`` that replays an already read ``body``.

    A request's stream can be read only once. The copy shares the request's
    scope, including ``request.state``, so later readers such as the A2A SDK
    see the same request and body. After the body, it passes on the
    connection's own messages, such as a client disconnect.
    """
    body_sent = False

    async def receive() -> Message:
        nonlocal body_sent
        if body_sent:
            return await request.receive()
        body_sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(request.scope, receive)
