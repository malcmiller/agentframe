"""A ``MessageDedupStore`` in Redis, shared by every host instance using it.

Install the ``redis`` extra (``pip install "agentframe-py[redis]"``) and pass
an application-owned ``redis.asyncio.Redis`` client. This module never
imports ``redis`` itself, so importing ``agentframe`` does not need it.
"""

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis


class RedisMessageDedupStore:
    """Keeps deduplication claims in Redis so all host instances share them.

    Each claim is one Redis string key that expires after the deduplication
    window. ``put_if_absent`` is a single ``SET key value NX GET PX ttl``
    command, so Redis itself decides which of two concurrent claims wins,
    across processes. ``SET`` with both ``NX`` and ``GET`` needs Redis 7.0
    or later (or a compatible server).

    The application owns the client: the store never closes it. Share a task
    store between the instances too, or a repeat that reaches another
    instance cannot find its original task and runs again.

    Args:
        client: A connected ``redis.asyncio.Redis`` client.
        key_prefix: Prepended to every key, so several agents can share one
            Redis database. Defaults to ``"agentframe:dedup:"``.
    """

    def __init__(self, client: "Redis", key_prefix: str = "agentframe:dedup:") -> None:
        self._client = client
        self._key_prefix = key_prefix

    async def put_if_absent(
        self, key: str, task_id: str, ttl_seconds: float
    ) -> str | None:
        """Store ``task_id`` unless ``key`` is present; return the stored ID if so."""
        # Redis expiries are whole milliseconds; round up so none is zero.
        ttl_ms = max(1, math.ceil(ttl_seconds * 1000))
        existing = await self._client.set(
            self._key_prefix + key, task_id, nx=True, get=True, px=ttl_ms
        )
        if existing is None:
            return None
        # The client returns bytes unless it was created with decode_responses.
        if isinstance(existing, bytes):
            return existing.decode()
        return str(existing)
